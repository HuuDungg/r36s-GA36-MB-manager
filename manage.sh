#!/bin/bash
# ======================================================================
# R36S Manager — Unified Management Script
# ======================================================================
# Provides options to:
#  1. Setup/install the virtual environment and dependencies.
#  2. Run the application from source.
#  3. Build the standalone app and DMG installer.
# ======================================================================

# Ensure we operate in the script's directory
cd "$(dirname "$0")"

# Colors for premium CLI styling
GREEN="\033[0;32m"
BLUE="\033[0;34m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
BOLD="\033[1m"
NC="\033[0m" # No Color

log_info() {
    echo -e "${BLUE}${BOLD}====>${NC} $1"
}

log_success() {
    echo -e "${GREEN}${BOLD}====>${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}${BOLD}====>${NC} $1"
}

log_error() {
    echo -e "${RED}${BOLD}====>${NC} $1"
}

check_python() {
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is not installed on this system. Please install it and try again."
        exit 1
    fi
}

setup_env() {
    log_info "Checking Python installation..."
    check_python
    
    log_info "Creating virtual environment in .venv..."
    python3 -m venv .venv
    
    log_info "Activating virtual environment..."
    source .venv/bin/activate
    
    log_info "Upgrading pip..."
    pip install --upgrade pip
    
    log_info "Installing dependencies from requirements.txt and pyinstaller..."
    pip install -r requirements.txt pyinstaller
    
    log_success "Virtual environment setup completed successfully!"
}

run_app() {
    if [ ! -d ".venv" ]; then
        log_warn "Virtual environment (.venv) not found. Setting it up first..."
        setup_env
    fi
    
    log_info "Activating virtual environment..."
    source .venv/bin/activate
    
    log_info "Launching R36S Manager..."
    python app.py
}

build_installer() {
    if [ ! -d ".venv" ]; then
        log_warn "Virtual environment (.venv) not found. Setting it up first..."
        setup_env
    fi
    
    log_info "Running the build script..."
    source .venv/bin/activate
    python build_app.py
}

show_help() {
    echo -e "${BOLD}R36S Manager CLI Utility${NC}"
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  setup   - Create virtual environment and install all dependencies"
    echo "  run     - Run the application from python source code"
    echo "  build   - Compile the standalone app and generate the DMG installer"
    echo "  help    - Show this help screen"
    echo ""
    echo "Run without arguments to open the interactive menu."
}

show_menu() {
    while true; do
        clear
        echo -e "${BLUE}${BOLD}======================================================================${NC}"
        echo -e "${BLUE}${BOLD}                       R36S Manager Control Panel                     ${NC}"
        echo -e "${BLUE}${BOLD}======================================================================${NC}"
        echo -e " 1) ${BOLD}Setup Virtual Environment${NC}   (Create .venv & install libraries)"
        echo -e " 2) ${BOLD}Run Application${NC}             (Run from Python source)"
        echo -e " 3) ${BOLD}Build Standalone DMG${NC}        (Create standalone .app & Installer)"
        echo -e " 4) ${BOLD}Exit${NC}"
        echo -e "${BLUE}${BOLD}======================================================================${NC}"
        echo -n "Choose an option [1-4]: "
        read -r opt
        
        case $opt in
            1)
                setup_env
                echo ""
                echo "Press enter to return to menu..."
                read -r
                ;;
            2)
                run_app
                echo ""
                echo "Press enter to return to menu..."
                read -r
                ;;
            3)
                build_installer
                echo ""
                echo "Press enter to return to menu..."
                read -r
                ;;
            4)
                log_info "Goodbye!"
                exit 0
                ;;
            *)
                log_error "Invalid option! Press enter to retry..."
                read -r
                ;;
        esac
    done
}

# Command dispatching
case "$1" in
    setup)
        setup_env
        ;;
    run)
        run_app
        ;;
    build)
        build_installer
        ;;
    help|--help|-h)
        show_help
        ;;
    "")
        show_menu
        ;;
    *)
        log_error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
