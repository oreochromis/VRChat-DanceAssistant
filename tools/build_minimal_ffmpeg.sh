#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK=/tmp/dance-ffmpeg-build
PREFIX="$WORK/prefix"
OUT="$ROOT/build-reports/minimal-ffmpeg"

mkdir -p "$WORK" "$OUT"
rm -f "$OUT/ffmpeg.exe" "$OUT/ffprobe.exe"
if [[ ! -d "$WORK/ffmpeg" ]]; then
    cp -a "$ROOT/build-tools/ffmpeg-n9.0.2" "$WORK/ffmpeg"
fi

export PATH="/ucrt64/bin:/usr/bin"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:/ucrt64/lib/pkgconfig"
export TMPDIR="$WORK/tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
mkdir -p "$TMPDIR"
export CPPFLAGS="-I$PREFIX/include"
export LDFLAGS="-L$PREFIX/lib -static"

if [[ ! -f "$PREFIX/lib/libx264.a" ]]; then
    rm -rf "$WORK/x264"
    cp -a "$ROOT/build-tools/x264-c24e06c2" "$WORK/x264"
    cd "$WORK/x264"
    ./configure \
        --prefix="$PREFIX" \
        --enable-static \
        --disable-shared \
        --disable-cli \
        --disable-opencl
    make -j16
    make install
fi

cd "$WORK/ffmpeg"
./configure \
    --prefix="$PREFIX" \
    --windres=false \
    --target-os=mingw32 \
    --arch=x86_64 \
    --enable-gpl \
    --enable-version3 \
    --enable-libx264 \
    --enable-libmfx \
    --enable-static \
    --disable-shared \
    --disable-debug \
    --disable-doc \
    --disable-iconv \
    --disable-ffplay \
    --enable-avdevice \
    --disable-everything \
    --enable-ffmpeg \
    --enable-ffprobe \
    --enable-decoder=h264,hevc,vp9,av1,aac,mp3,opus,vorbis,rawvideo,wrapped_avframe \
    --enable-encoder=libx264,h264_nvenc,h264_qsv,h264_amf,aac,rawvideo,wrapped_avframe,pcm_s16le \
    --enable-demuxer=mov,rawvideo \
    --enable-indev=lavfi \
    --enable-muxer=mp4,null,rawvideo \
    --enable-protocol=file,pipe,http,https,tcp,tls \
    --enable-filter=scale,color,format,null,ssim,aresample \
    --enable-schannel

for component in LIBX264_ENCODER H264_NVENC_ENCODER H264_QSV_ENCODER H264_AMF_ENCODER AAC_ENCODER WRAPPED_AVFRAME_DECODER WRAPPED_AVFRAME_ENCODER PCM_S16LE_ENCODER MOV_DEMUXER LAVFI_INDEV MP4_MUXER HTTPS_PROTOCOL ARESAMPLE_FILTER; do
    if ! grep -q "^CONFIG_${component}=yes$" ffbuild/config.mak; then
        printf 'Required component is missing: %s\n' "$component" >&2
        exit 1
    fi
done

make -j16
make install

cp "$PREFIX/bin/ffmpeg.exe" "$OUT/ffmpeg.exe"
cp "$PREFIX/bin/ffprobe.exe" "$OUT/ffprobe.exe"
strip --strip-all "$OUT/ffmpeg.exe" "$OUT/ffprobe.exe"
cp "$WORK/ffmpeg/COPYING.GPLv3" "$OUT/FFmpeg-LICENSE.txt"
cp "$ROOT/build-tools/x264-c24e06c2/COPYING" "$OUT/x264-LICENSE.txt"
cp "$ROOT/build-tools/licenses/mfx_dispatch-LICENSE.txt" "$OUT/mfx_dispatch-LICENSE.txt"
cp /ucrt64/share/licenses/zlib/LICENSE "$OUT/zlib-LICENSE.txt"
cp /ucrt64/share/licenses/amf-headers/LICENSE "$OUT/AMF-headers-LICENSE.txt"
cp /ucrt64/share/licenses/ffnvcodec-headers/LICENSE "$OUT/ffnvcodec-headers-LICENSE.txt"
cp /ucrt64/share/licenses/libwinpthread/COPYING "$OUT/winpthreads-LICENSE.txt"
cp /ucrt64/share/licenses/gcc/COPYING.RUNTIME "$OUT/GCC-RUNTIME-EXCEPTION.txt"

for binary in "$OUT/ffmpeg.exe" "$OUT/ffprobe.exe"; do
    if ldd "$binary" | grep -E 'ucrt64|msys64|not found'; then
        printf 'Non-system DLL dependency remains in %s\n' "$binary" >&2
        exit 1
    fi
done

printf 'Candidate binaries written to %s\n' "$OUT"
ls -lh "$OUT"
