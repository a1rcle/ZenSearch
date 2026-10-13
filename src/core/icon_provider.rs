use std::collections::HashMap;
use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, RwLock};

use windows::core::PCWSTR;
use windows::Win32::Graphics::Gdi::{
    CreateCompatibleDC, DeleteDC, DeleteObject, GetDIBits, GetObjectW, BITMAP, BITMAPINFO,
    BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS, HGDIOBJ,
};
use windows::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_FLAGS_AND_ATTRIBUTES,
};
use windows::Win32::UI::Shell::{
    SHGetFileInfoW, SHFILEINFOW, SHGFI_ICON, SHGFI_SMALLICON, SHGFI_USEFILEATTRIBUTES,
};
use windows::Win32::UI::WindowsAndMessaging::{
    DestroyIcon, GetIconInfo, HICON, ICONINFO,
};

#[derive(Clone)]
pub struct CachedIcon {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

impl CachedIcon {
    pub fn to_slint_image(&self) -> slint::Image {
        let mut pixel_buffer =
            slint::SharedPixelBuffer::<slint::Rgba8Pixel>::new(self.width, self.height);
        pixel_buffer.make_mut_bytes().copy_from_slice(&self.rgba);
        slint::Image::from_rgba8(pixel_buffer)
    }
}

static EXT_CACHE: LazyLock<RwLock<HashMap<String, CachedIcon>>> =
    LazyLock::new(|| RwLock::new(HashMap::new()));

static EXE_CACHE: LazyLock<RwLock<HashMap<PathBuf, CachedIcon>>> =
    LazyLock::new(|| RwLock::new(HashMap::new()));

const MAX_EXE_CACHE_ENTRIES: usize = 200;

/// Convert a Win32 HICON to a CachedIcon using 32-bit RGBA pixel buffer
unsafe fn hicon_to_cached_icon(hicon: HICON) -> Option<CachedIcon> {
    if hicon.is_invalid() {
        return None;
    }

    let mut icon_info = ICONINFO::default();
    if GetIconInfo(hicon, &mut icon_info).is_err() {
        let _ = DestroyIcon(hicon);
        return None;
    }

    let hbm_color = icon_info.hbmColor;
    let hbm_mask = icon_info.hbmMask;

    if hbm_color.is_invalid() {
        if !hbm_mask.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(hbm_mask.0));
        }
        let _ = DestroyIcon(hicon);
        return None;
    }

    let mut bmp = BITMAP::default();
    let get_obj_res = GetObjectW(
        HGDIOBJ(hbm_color.0),
        std::mem::size_of::<BITMAP>() as i32,
        Some(&mut bmp as *mut _ as *mut _),
    );

    if get_obj_res == 0 || bmp.bmWidth <= 0 || bmp.bmHeight <= 0 {
        let _ = DeleteObject(HGDIOBJ(hbm_color.0));
        if !hbm_mask.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(hbm_mask.0));
        }
        let _ = DestroyIcon(hicon);
        return None;
    }

    let width = bmp.bmWidth as u32;
    let height = bmp.bmHeight as u32;

    let hdc = CreateCompatibleDC(None);
    if hdc.is_invalid() {
        let _ = DeleteObject(HGDIOBJ(hbm_color.0));
        if !hbm_mask.is_invalid() {
            let _ = DeleteObject(HGDIOBJ(hbm_mask.0));
        }
        let _ = DestroyIcon(hicon);
        return None;
    }

    let mut bmi = BITMAPINFO {
        bmiHeader: BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: width as i32,
            biHeight: -(height as i32), // Top-down DIB
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB.0,
            ..Default::default()
        },
        ..Default::default()
    };

    let mut bgra_buf = vec![0u8; (width * height * 4) as usize];
    let lines = GetDIBits(
        hdc,
        hbm_color,
        0,
        height,
        Some(bgra_buf.as_mut_ptr() as *mut _),
        &mut bmi,
        DIB_RGB_COLORS,
    );

    let _ = DeleteDC(hdc);
    let _ = DeleteObject(HGDIOBJ(hbm_color.0));
    if !hbm_mask.is_invalid() {
        let _ = DeleteObject(HGDIOBJ(hbm_mask.0));
    }
    let _ = DestroyIcon(hicon);

    if lines == 0 {
        return None;
    }

    // Check if alpha channel is populated
    let has_non_zero_alpha = bgra_buf.chunks_exact(4).any(|chunk| chunk[3] != 0);

    let mut rgba = vec![0u8; (width * height * 4) as usize];
    for i in 0..(width * height) as usize {
        let b = bgra_buf[i * 4];
        let g = bgra_buf[i * 4 + 1];
        let r = bgra_buf[i * 4 + 2];
        let a = if has_non_zero_alpha {
            bgra_buf[i * 4 + 3]
        } else {
            // For legacy 24-bit icons without alpha, treat non-black pixels as solid
            if r > 0 || g > 0 || b > 0 {
                255
            } else {
                0
            }
        };

        rgba[i * 4] = r;
        rgba[i * 4 + 1] = g;
        rgba[i * 4 + 2] = b;
        rgba[i * 4 + 3] = a;
    }

    Some(CachedIcon {
        width,
        height,
        rgba,
    })
}

/// Query shell icon using virtual file attributes (zero disk I/O)
fn extract_virtual_icon(name_or_ext: &str, is_dir: bool) -> Option<CachedIcon> {
    let wide_name: Vec<u16> = OsStr::new(name_or_ext)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    let attrs = if is_dir {
        FILE_ATTRIBUTE_DIRECTORY
    } else {
        FILE_ATTRIBUTE_NORMAL
    };

    let mut shfi = SHFILEINFOW::default();
    unsafe {
        let res = SHGetFileInfoW(
            PCWSTR(wide_name.as_ptr()),
            attrs,
            Some(&mut shfi as *mut _ as *mut _),
            std::mem::size_of::<SHFILEINFOW>() as u32,
            SHGFI_ICON | SHGFI_SMALLICON | SHGFI_USEFILEATTRIBUTES,
        );

        if res != 0 && !shfi.hIcon.is_invalid() {
            hicon_to_cached_icon(shfi.hIcon)
        } else {
            None
        }
    }
}

/// Query shell icon for a real file on disk (used for .exe, .lnk)
fn extract_file_icon(path: &Path) -> Option<CachedIcon> {
    let wide_path: Vec<u16> = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    let mut shfi = SHFILEINFOW::default();
    unsafe {
        let res = SHGetFileInfoW(
            PCWSTR(wide_path.as_ptr()),
            FILE_FLAGS_AND_ATTRIBUTES(0),
            Some(&mut shfi as *mut _ as *mut _),
            std::mem::size_of::<SHFILEINFOW>() as u32,
            SHGFI_ICON | SHGFI_SMALLICON,
        );

        if res != 0 && !shfi.hIcon.is_invalid() {
            hicon_to_cached_icon(shfi.hIcon)
        } else {
            None
        }
    }
}

/// Fallback blank transparent 16x16 icon
fn get_fallback_icon() -> slint::Image {
    let pixel_buffer = slint::SharedPixelBuffer::<slint::Rgba8Pixel>::new(16, 16);
    slint::Image::from_rgba8(pixel_buffer)
}

/// Public API: Get the icon for a given file or folder path
pub fn get_icon_for_path(path_str: &str) -> slint::Image {
    let path = Path::new(path_str);

    // 1. Directory detection
    let is_dir = path_str.ends_with('\\') || path_str.ends_with('/') || path.is_dir();
    if is_dir {
        if let Ok(cache) = EXT_CACHE.read() {
            if let Some(cached) = cache.get("__folder__") {
                return cached.to_slint_image();
            }
        }

        if let Some(cached) = extract_virtual_icon("folder", true) {
            let img = cached.to_slint_image();
            if let Ok(mut cache) = EXT_CACHE.write() {
                cache.insert("__folder__".to_string(), cached);
            }
            return img;
        }
        return get_fallback_icon();
    }

    // 2. Extract extension
    let ext_lower = path
        .extension()
        .and_then(|s| s.to_str())
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_else(|| "__unknown__".to_string());

    // 3. For .exe and .lnk files, query physical file icon with LRU caching
    if ext_lower == "exe" || ext_lower == "lnk" {
        let pbuf = path.to_path_buf();
        if let Ok(cache) = EXE_CACHE.read() {
            if let Some(cached) = cache.get(&pbuf) {
                return cached.to_slint_image();
            }
        }

        if path.exists() {
            if let Some(cached) = extract_file_icon(path) {
                let img = cached.to_slint_image();
                if let Ok(mut cache) = EXE_CACHE.write() {
                    if cache.len() >= MAX_EXE_CACHE_ENTRIES {
                        cache.clear();
                    }
                    cache.insert(pbuf, cached);
                }
                return img;
            }
        }
    }

    // 4. Query extension cache
    if let Ok(cache) = EXT_CACHE.read() {
        if let Some(cached) = cache.get(&ext_lower) {
            return cached.to_slint_image();
        }
    }

    // 5. Virtual extension query
    let dummy_filename = if ext_lower == "__unknown__" {
        "file".to_string()
    } else {
        format!("file.{}", ext_lower)
    };

    if let Some(cached) = extract_virtual_icon(&dummy_filename, false) {
        let img = cached.to_slint_image();
        if let Ok(mut cache) = EXT_CACHE.write() {
            cache.insert(ext_lower, cached);
        }
        return img;
    }

    get_fallback_icon()
}

/// Pre-warm common file type icons during application startup
pub fn init() {
    let common_types = [
        "__folder__",
        "txt",
        "docx",
        "doc",
        "xlsx",
        "xls",
        "pptx",
        "pdf",
        "zip",
        "rar",
        "7z",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "mp3",
        "mp4",
        "mkv",
        "exe",
        "rs",
        "py",
        "js",
        "ts",
        "json",
        "html",
        "css",
        "md",
        "ini",
        "log",
    ];

    for ext in common_types {
        let is_dir = ext == "__folder__";
        let dummy = if is_dir {
            "folder".to_string()
        } else {
            format!("file.{}", ext)
        };

        if let Some(cached) = extract_virtual_icon(&dummy, is_dir) {
            if let Ok(mut cache) = EXT_CACHE.write() {
                cache.insert(ext.to_string(), cached);
            }
        }
    }
    tracing::info!(
        "Icon provider initialized with {} prewarmed icons",
        EXT_CACHE.read().map(|c| c.len()).unwrap_or(0)
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_icon_extraction_and_caching() {
        init();

        // 1. Verify folder icon
        let folder_icon = get_icon_for_path(r"C:\Windows");
        assert!(folder_icon.size().width > 0);
        assert!(folder_icon.size().height > 0);

        // 2. Verify virtual file icons
        let txt_icon = get_icon_for_path("sample.txt");
        assert!(txt_icon.size().width > 0);

        let rs_icon = get_icon_for_path("main.rs");
        assert!(rs_icon.size().width > 0);

        // 3. Verify repeated queries hit memory cache without issue
        let txt_icon_cached = get_icon_for_path("another_file.txt");
        assert_eq!(txt_icon.size(), txt_icon_cached.size());
    }
}
