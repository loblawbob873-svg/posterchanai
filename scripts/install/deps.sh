#!/bin/bash
# Dependency Checking and Installation Instructions
# Sourced by install.sh

check_dependencies() {
    print_step "Checking system dependencies..."

    MISSING_DEPS=""

    # Check for Python 3.10+
    if ! command -v python3 &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS python3"
    else
        PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        PY_OK=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)")
        if [ "$PY_OK" = "1" ]; then
            print_success "Python $PY_VERSION"
        else
            print_warning "Python $PY_VERSION detected. Python 3.10+ recommended."
        fi
    fi

    # Check for pip
    if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null 2>&1; then
        MISSING_DEPS="$MISSING_DEPS pip"
    fi

    # Check for gcc (needed to compile llama-cpp-python)
    if ! command -v gcc &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS gcc"
    fi

    # Check for cmake
    if ! command -v cmake &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS cmake"
    fi

    # Check for git
    if ! command -v git &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS git"
    fi

    if [ -n "$MISSING_DEPS" ]; then
        print_error "Missing dependencies:$MISSING_DEPS"
        echo ""
        show_install_instructions
        exit 1
    fi

    print_success "All base dependencies found"

    # Check optional dependencies
    if ! command -v ffmpeg &>/dev/null; then
        print_warning "ffmpeg not found - music transcoding, video compression, the 'hava' effect and the LIVE-STREAM BITRATE CLAMP will be unavailable"
        echo "  Install ffmpeg for music streaming, the 'compress'/'clip' commands (video) and the 'hava' image→song video"
        # Without ffmpeg the clamp can't run, so viewers are served whatever the streamer's encoder sends —
        # every viewer then costs the FULL source bitrate of your upload. Streams still work; they just
        # cost a lot more bandwidth, which is easy to miss because nothing visibly breaks.
        echo "  It is also required to clamp live streams to 720p30 — without it every viewer pulls the streamer's full bitrate"
    else
        print_success "ffmpeg found (music transcoding + video compression + 'hava' video + live-stream clamp available)"
    fi

    # tesseract OCR binary - pytesseract (in requirements.txt) is only a wrapper and
    # needs the system `tesseract` command to read text from images/scanned PDFs.
    if ! command -v tesseract &>/dev/null; then
        print_warning "tesseract not found - OCR (text from images/scanned PDFs) will be unavailable"
        case "$DISTRO" in
            gentoo) echo "  Install with: emerge -av app-text/tesseract  (set LINGUAS for language data, e.g. LINGUAS=\"en th zh-CN ja ko ar ru hi es fr de\")" ;;
            arch)   echo "  Install with: pacman -S tesseract tesseract-data-eng tesseract-data-tha tesseract-data-chi_sim tesseract-data-jpn tesseract-data-kor tesseract-data-ara tesseract-data-rus tesseract-data-hin tesseract-data-spa tesseract-data-fra tesseract-data-deu" ;;
            debian) echo "  Install with: apt install tesseract-ocr tesseract-ocr-tha tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn tesseract-ocr-kor tesseract-ocr-ara tesseract-ocr-rus tesseract-ocr-hin tesseract-ocr-spa tesseract-ocr-fra tesseract-ocr-deu" ;;
            fedora) echo "  Install with: dnf install tesseract tesseract-langpack-tha tesseract-langpack-chi_sim tesseract-langpack-chi_tra tesseract-langpack-jpn tesseract-langpack-kor tesseract-langpack-ara tesseract-langpack-rus tesseract-langpack-hin tesseract-langpack-spa tesseract-langpack-fra tesseract-langpack-deu" ;;
            suse)   echo "  Install with: zypper install tesseract-ocr tesseract-ocr-traineddata-english tesseract-ocr-traineddata-thai tesseract-ocr-traineddata-chinese_simplified tesseract-ocr-traineddata-japanese tesseract-ocr-traineddata-korean tesseract-ocr-traineddata-arabic tesseract-ocr-traineddata-russian tesseract-ocr-traineddata-hindi tesseract-ocr-traineddata-spanish tesseract-ocr-traineddata-french tesseract-ocr-traineddata-german" ;;
            *)      echo "  Install tesseract + language packs (eng tha chi_sim chi_tra jpn kor ara rus hin spa fra deu) for your distribution" ;;
        esac
        echo "  (Language packs beyond English are needed to OCR/translate non-Latin images — Thai, Chinese, etc.)"
    else
        # OCR/translate of non-Latin images needs the matching traineddata; base tesseract ships eng only.
        _langs="$(tesseract --list-langs 2>/dev/null | tail -n +2 | tr '\n' ' ')"
        if [ -z "$_langs" ]; then
            # Couldn't enumerate languages (older tesseract, permissions, or genuinely no traineddata
            # installed). Don't claim all-good and don't misdirect a provisioned host into reinstalling —
            # just note it so the operator can confirm the packs are present.
            print_success "tesseract found (OCR available — could not list languages; ensure eng + non-Latin packs are installed)"
        elif ! echo " $_langs " | grep -q " tha "; then
            print_warning "tesseract found but only English data - non-Latin OCR/translate (Thai, Chinese, …) will fail"
            case "$DISTRO" in
                gentoo) echo "  Add language data via LINGUAS (e.g. LINGUAS=\"th zh-CN zh-TW ja ko ar ru hi es fr de\") then re-emerge app-text/tesseract" ;;
                arch)   echo "  Install with: pacman -S tesseract-data-tha tesseract-data-chi_sim tesseract-data-chi_tra tesseract-data-jpn tesseract-data-kor tesseract-data-ara tesseract-data-rus tesseract-data-hin tesseract-data-spa tesseract-data-fra tesseract-data-deu" ;;
                debian) echo "  Install with: apt install tesseract-ocr-tha tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn tesseract-ocr-kor tesseract-ocr-ara tesseract-ocr-rus tesseract-ocr-hin tesseract-ocr-spa tesseract-ocr-fra tesseract-ocr-deu" ;;
                fedora) echo "  Install with: dnf install tesseract-langpack-tha tesseract-langpack-chi_sim tesseract-langpack-chi_tra tesseract-langpack-jpn tesseract-langpack-kor tesseract-langpack-ara tesseract-langpack-rus tesseract-langpack-hin tesseract-langpack-spa tesseract-langpack-fra tesseract-langpack-deu" ;;
                suse)   echo "  Install with: zypper install tesseract-ocr-traineddata-thai tesseract-ocr-traineddata-chinese_simplified tesseract-ocr-traineddata-japanese tesseract-ocr-traineddata-korean tesseract-ocr-traineddata-arabic tesseract-ocr-traineddata-russian tesseract-ocr-traineddata-hindi tesseract-ocr-traineddata-spanish tesseract-ocr-traineddata-french tesseract-ocr-traineddata-german" ;;
                *)      echo "  Install tesseract language packs (tha chi_sim chi_tra jpn kor ara rus hin spa fra deu) for your distribution" ;;
            esac
        else
            print_success "tesseract found (OCR available, multi-language)"
        fi
    fi

    # Browser for the 'screenshot' command. Chrome/Chromium is preferred (driven over
    # the DevTools protocol — full-page and JS-aware so SPAs render); Firefox is a
    # fallback (its --screenshot fires before JS paints, so SPAs come out blank).
    if command -v google-chrome-stable &>/dev/null || command -v google-chrome &>/dev/null \
       || command -v chromium &>/dev/null || command -v chromium-browser &>/dev/null; then
        print_success "Chrome/Chromium found (screenshot command available, JS-aware)"
    elif command -v firefox &>/dev/null || command -v firefox-bin &>/dev/null; then
        print_warning "only Firefox found - screenshots work but JS-heavy sites (SPAs) may be blank"
        echo "  For reliable screenshots install Chrome/Chromium:"
        case "$DISTRO" in
            gentoo) echo "    emerge -av www-client/google-chrome  (or www-client/chromium)" ;;
            arch)   echo "    pacman -S chromium" ;;
            debian) echo "    apt install chromium  (or install google-chrome-stable)" ;;
            fedora) echo "    dnf install chromium" ;;
            suse)   echo "    zypper install chromium" ;;
            *)      echo "    install google-chrome-stable or chromium for your distribution" ;;
        esac
    else
        print_warning "no browser found - the 'screenshot' command will be unavailable"
        case "$DISTRO" in
            gentoo) echo "  Install with: emerge -av www-client/google-chrome" ;;
            arch)   echo "  Install with: pacman -S chromium" ;;
            debian) echo "  Install with: apt install chromium  (or google-chrome-stable)" ;;
            fedora) echo "  Install with: dnf install chromium" ;;
            suse)   echo "  Install with: zypper install chromium" ;;
            *)      echo "  Install google-chrome-stable or chromium for your distribution" ;;
        esac
    fi

    # Color emoji font. Without one, the browser renders emoji as tofu boxes (□) in
    # screenshots and in the post-card images (emoji in post text). Checked via
    # fontconfig (fc-list), so it needs fontconfig present too.
    if command -v fc-list &>/dev/null && fc-list 2>/dev/null | grep -qi emoji; then
        print_success "color emoji font found (emoji render in screenshots/post-cards)"
    else
        print_warning "no color emoji font - emoji show as boxes in screenshots/post cards"
        case "$DISTRO" in
            gentoo) echo "  Install with: emerge -av media-fonts/noto-emoji && fc-cache -f" ;;
            arch)   echo "  Install with: pacman -S noto-fonts-emoji && fc-cache -f" ;;
            debian) echo "  Install with: apt install fonts-noto-color-emoji && fc-cache -f" ;;
            fedora) echo "  Install with: dnf install google-noto-emoji-color-fonts && fc-cache -f" ;;
            suse)   echo "  Install with: zypper install noto-coloremoji-fonts && fc-cache -f" ;;
            *)      echo "  Install a color emoji font (e.g. Noto Color Emoji) for your distribution" ;;
        esac
    fi

    # Bold TrueType font for the text Effects (meme/gay/blacked/kosher/barked — outlined
    # caption text & the BLACKED wordmark). Pillow's bundled default works as a fallback,
    # but a real bold face (DejaVu/Liberation/Impact) reads much better; the BLACKED
    # wordmark in particular prefers the Helvetica-clone Liberation Sans Bold. Checked via
    # fontconfig.
    if command -v fc-list &>/dev/null && fc-list 2>/dev/null | grep -qiE "dejavu sans:.*bold|liberation sans:.*bold|impact"; then
        print_success "bold TrueType font found (meme/effects text renders cleanly)"
    else
        print_warning "no bold sans font - the text Effects fall back to Pillow's basic font"
        case "$DISTRO" in
            gentoo) echo "  Install with: emerge -av media-fonts/dejavu && fc-cache -f" ;;
            arch)   echo "  Install with: pacman -S ttf-dejavu && fc-cache -f" ;;
            debian) echo "  Install with: apt install fonts-dejavu && fc-cache -f" ;;
            fedora) echo "  Install with: dnf install dejavu-sans-fonts && fc-cache -f" ;;
            suse)   echo "  Install with: zypper install dejavu-fonts && fc-cache -f" ;;
            *)      echo "  Install DejaVu Sans (or Liberation Sans) Bold for your distribution" ;;
        esac
    fi

    # Check for pax-utils (scanelf) - needed for Intel Arc on hardened kernels
    if [ "$BACKEND" = "intel" ]; then
        if ! command -v scanelf &>/dev/null; then
            print_warning "scanelf (pax-utils) not found"
            echo "  Required to fix IPEX library permissions on hardened kernels (Gentoo)"
            case "$DISTRO" in
                gentoo) echo "  Install with: emerge -av app-misc/pax-utils" ;;
                arch) echo "  Install with: pacman -S pax-utils" ;;
                debian) echo "  Install with: apt install pax-utils" ;;
                fedora) echo "  Install with: dnf install pax-utils" ;;
                suse) echo "  Install with: zypper install pax-utils" ;;
                *) echo "  Install pax-utils for your distribution" ;;
            esac
        else
            print_success "scanelf found (pax-utils)"
        fi
    fi
}

show_install_instructions() {
    detect_distro

    echo -e "${YELLOW}Please install the required packages:${NC}"
    echo ""

    case "$DISTRO" in
        gentoo)
            show_gentoo_instructions
            ;;
        arch)
            show_arch_instructions
            ;;
        debian)
            show_debian_instructions
            ;;
        fedora)
            show_fedora_instructions
            ;;
        suse)
            show_suse_instructions
            ;;
        *)
            echo "  Please install: python3, pip, cmake, gcc, git"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  # See: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
            ;;
    esac
    echo ""
}

show_gentoo_instructions() {
    echo -e "${BOLD}Gentoo Linux:${NC}"
    echo ""
    echo "  # Base dependencies"
    echo "  emerge -av dev-lang/python dev-python/pip dev-build/cmake sys-devel/gcc"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  echo -e 'dev-build/rocm-cmake\ndev-util/hipcc\ndev-libs/rocm-core\ndev-libs/roct-thunk-interface\ndev-libs/rocm-device-libs\ndev-libs/rocr-runtime\ndev-libs/rocm-comgr\ndev-util/rocminfo\ndev-util/rocm-smi\ndev-libs/rocm-opencl-runtime\ndev-util/hip\nsci-libs/hipBLAS\nsci-libs/hipBLAS-common\nsci-libs/rocBLAS\nsci-libs/rocSOLVER\ndev-util/Tensile' | sudo tee /etc/portage/package.accept_keywords/rocm"
    echo "  emerge -av dev-libs/rocm-opencl-runtime dev-util/hip dev-libs/rocr-runtime sci-libs/hipBLAS"
    echo ""
    echo "  # For Intel Arc GPU - OS GPU runtime (portage):"
    echo "  echo 'dev-util/intel-graphics-compiler no-distcc.conf' | sudo tee -a /etc/portage/package.env/intel-graphics-compiler"
    echo "  emerge -av dev-libs/intel-compute-runtime dev-libs/level-zero media-libs/gmmlib \\"
    echo "             dev-util/patchelf app-misc/pax-utils"
    echo "  # pax-utils=scanelf (fix IPEX perms on hardened kernels); patchelf for glibc 2.41+"
    echo "  # Pin the runtime ~amd64 so @world won't downgrade it (see docs/IPEX-LLM-SETUP.md)."
    echo "  # oneAPI Base Toolkit 2025.0 is needed to BUILD the SYCL llama-cpp (LLM):"
    echo "  #   https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
    echo "  # CRITICAL: portage's IGC (<=2.35.2) is NOT enough. After emerge, install IGC 2.35.5:"
    echo "  #   sudo ./scripts/install-igc.sh        # unblocks LLM 14B/long-ctx AND image gen >=768"
    echo ""
    echo "  # For NVIDIA GPU:"
    echo "  emerge -av x11-drivers/nvidia-drivers dev-util/nvidia-cuda-toolkit"
}

show_arch_instructions() {
    echo -e "${BOLD}Arch Linux:${NC}"
    echo "  pacman -S python python-pip cmake gcc git"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  pacman -S rocm-hip-sdk rocm-opencl-sdk"
    echo ""
    echo ""
    echo "  # For Intel Arc GPU - OS GPU runtime:"
    echo "  pacman -S intel-compute-runtime level-zero-loader intel-graphics-compiler \\"
    echo "            patchelf pax-utils"
    echo "  # oneAPI Base Toolkit 2025.0 (AUR: intel-oneapi-basekit) to build the SYCL llama-cpp."
    echo "  # CRITICAL: install IGC 2.35.5 on top (distro IGC is older): sudo ./scripts/install-igc.sh"
    echo "  # For NVIDIA GPU: pacman -S nvidia cuda"
}

show_debian_instructions() {
    echo -e "${BOLD}Debian/Ubuntu:${NC}"
    echo "  apt install python3 python3-pip python3-venv cmake build-essential git patchelf pax-utils"
    echo ""
    echo "  # For Intel Arc GPU - OS GPU runtime (Intel's apt repo gives current NEO/L0/gmmlib):"
    echo "  #   https://dgpu-docs.intel.com/driver/installation.html  (adds intel-opencl-icd,"
    echo "  #   libze1/libze-intel-gpu1, intel-level-zero-gpu, libigdgmm12)"
    echo "  # oneAPI Base Toolkit 2025.0 (apt: intel-basekit) to build the SYCL llama-cpp (LLM)."
    echo "  # CRITICAL: distro/Intel-repo IGC is too old - install IGC 2.35.5 on top:"
    echo "  #   sudo ./scripts/install-igc.sh --download   # fetches the v2.35.5 debs from GitHub"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/\$(lsb_release -cs)/amdgpu-install_6.0.60002-1_all.deb"
    echo "  apt install ./amdgpu-install_*.deb"
    echo "  amdgpu-install --usecase=rocm"
    echo ""
    echo "  # For NVIDIA GPU: apt install nvidia-driver nvidia-cuda-toolkit"
}

show_fedora_instructions() {
    echo -e "${BOLD}Fedora:${NC}"
    echo "  dnf install python3 python3-pip cmake gcc-c++ git patchelf pax-utils"
    echo ""
    echo "  # For Intel Arc GPU - OS GPU runtime:"
    echo "  dnf install intel-compute-runtime level-zero oneapi-level-zero intel-gmmlib"
    echo "  # (Fedora ships these; on RHEL use Intel's dnf repo per dgpu-docs.intel.com)"
    echo "  # oneAPI Base Toolkit 2025.0 (dnf: intel-basekit) to build the SYCL llama-cpp."
    echo "  # CRITICAL: install IGC 2.35.5 on top: sudo ./scripts/install-igc.sh --download"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  dnf install https://repo.radeon.com/amdgpu-install/latest/rhel/\$(rpm -E %rhel)/amdgpu-install-*.noarch.rpm"
    echo "  amdgpu-install --usecase=rocm"
    echo ""
    echo "  # For NVIDIA GPU: dnf install nvidia-driver cuda"
}

show_suse_instructions() {
    echo -e "${BOLD}openSUSE (Leap / Tumbleweed):${NC}"
    echo "  zypper install python311 python311-pip cmake gcc-c++ git patchelf pax-utils"
    echo ""
    echo "  # For Intel Arc GPU - OS GPU runtime:"
    echo "  zypper install intel-compute-runtime level-zero libze1 libigdgmm12"
    echo "  # (Tumbleweed has current packages; Leap may need Intel's repo per dgpu-docs.intel.com)"
    echo "  # oneAPI Base Toolkit 2025.0 to build the SYCL llama-cpp (LLM):"
    echo "  #   add Intel's oneAPI zypper repo, then: zypper install intel-basekit"
    echo "  # CRITICAL: distro IGC is too old - install IGC 2.35.5 on top:"
    echo "  #   sudo ./scripts/install-igc.sh --download   # fetches v2.35.5 .debs, extracts to libdir"
    echo ""
    echo "  # For NVIDIA GPU: zypper install nvidia-video-G06 nvidia-compute-utils-G06 (+ CUDA repo)"
}
