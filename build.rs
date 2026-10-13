use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn main() {
    // 1. Compile Slint UI components
    slint_build::compile("ui/main.slint").expect("Failed to compile ui/main.slint");

    // 2. Configure native link search paths for Everything-SDK
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    println!("cargo:rustc-link-search=native={}/tools", manifest_dir);
    println!("cargo:rustc-link-search=native={}/Everything-SDK/lib", manifest_dir);
    println!("cargo:rustc-link-lib=dylib=Everything64");

    // 3. Ensure Everything64.dll is copied to the target directory for test/debug runtimes
    if let Ok(out_dir) = env::var("OUT_DIR") {
        let out_path = PathBuf::from(&out_dir);
        // OUT_DIR is target/debug/build/<pkg>/out -> navigate up to target/debug
        if let Some(target_dir) = out_path.ancestors().nth(3) {
            let src_dll = Path::new(&manifest_dir).join("tools").join("Everything64.dll");
            if src_dll.exists() {
                let _ = fs::copy(&src_dll, target_dir.join("Everything64.dll"));
                let deps_dir = target_dir.join("deps");
                if deps_dir.exists() {
                    let _ = fs::copy(&src_dll, deps_dir.join("Everything64.dll"));
                }
            }
        }
    }

    // 4. Link Windows resource file (icon and application metadata)
    let res_file = Path::new(&manifest_dir).join("assets").join("zensearch.res");
    if res_file.exists() {
        println!("cargo:rustc-link-arg={}", res_file.display());
    }
    println!("cargo:rerun-if-changed=assets/logo.ico");
    println!("cargo:rerun-if-changed=assets/zensearch.rc");
    println!("cargo:rerun-if-changed=assets/zensearch.res");
}
