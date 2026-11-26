#!/bin/sh
set -eu

install_wheels() {
    if [ "${EFIR_CI_TOOLS_READY:-}" = 1 ]; then return; fi
    python -m pip --disable-pip-version-check install --only-binary=:all: "$@"
}

setup_media() {
    if [ "${EFIR_CI_TOOLS_READY:-}" = 1 ]; then return; fi
    apt-get update -qq
    apt-get install -y --no-install-recommends xvfb xauth tk
    install_wheels unittest-xml-reporting==3.2.0 coverage==7.13.5 imageio-ffmpeg==0.6.0
    EFIR_FFMPEG=$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')
    EFIR_TEST_DESKTOP=1
    export EFIR_FFMPEG EFIR_TEST_DESKTOP
}

case "${1:-}" in
    quality)
        install_wheels ruff==0.15.7
        python -m ruff check efir tests scripts
        python -m compileall -q efir tests scripts
        ;;
    unit)
        install_wheels unittest-xml-reporting==3.2.0
        python -m xmlrunner discover -s tests -p test_project.py -v -o reports/qa/junit
        python -m xmlrunner discover -s tests -p test_core_edges.py -v -o reports/qa/junit
        ;;
    integration)
        setup_media
        python -m xmlrunner discover -s tests -p test_render.py -v -o reports/qa/junit
        xvfb-run -a python -m xmlrunner discover -s tests -p test_entry_points.py -v -o reports/qa/junit
        xvfb-run -a python -m xmlrunner discover -s tests -p test_desktop.py -v -o reports/qa/junit
        ;;
    e2e)
        setup_media
        python -m xmlrunner discover -s tests -p test_cli_e2e.py -v -o reports/qa/junit
        ;;
    coverage)
        setup_media
        tests_status=0
        xvfb-run -a python -m coverage run -m unittest discover -s tests -v || tests_status=$?
        python scripts/coverage_report.py
        exit "$tests_status"
        ;;
    *)
        printf 'Unexpected EFIR CI stage: %s\n' "${1:-}" >&2
        exit 2
        ;;
esac
