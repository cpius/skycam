#!/bin/zsh
# Build SkyCam, install it on the iPhone and launch it.
#
#   ./install.sh                      # debug build -> MD iPhone Air, then launch
#   ./install.sh --config Release --no-launch
#   ./install.sh --device <udid>
#
# Free (Personal Team) signing: the install stops working after 7 days -- just run this again.
set -euo pipefail
cd "${0:A:h}"

DEVICE=00008150-00124D061A38401C
CONFIG=Debug
LAUNCH=1
while (( $# )); do
  case $1 in
    --device) DEVICE=$2; shift 2 ;;
    --config) CONFIG=$2; shift 2 ;;
    --no-launch) LAUNCH=0; shift ;;
    *) echo "unknown flag $1"; exit 2 ;;
  esac
done

echo "[$(date +%T)] generating project"
xcodegen generate --quiet
echo "[$(date +%T)] building $CONFIG (log: build.log)"
set +e
xcodebuild -project SkyCam.xcodeproj -scheme SkyCam -configuration "$CONFIG" \
  -destination "id=$DEVICE" -derivedDataPath build -allowProvisioningUpdates \
  build 2>&1 | tee build.log | grep --line-buffered -E "^(SwiftCompile|SwiftDriver|Ld|CodeSign|ProcessProductPackaging|Signing)|error:|warning: .*SkyCam/|\*\* BUILD"
st=(${pipestatus[@]})
set -e
[[ ${st[1]} == 0 ]] || { echo "build failed -- see build.log"; exit 1; }

APP=build/Build/Products/$CONFIG-iphoneos/SkyCam.app
echo "[$(date +%T)] installing $APP on $DEVICE"
xcrun devicectl device install app --device "$DEVICE" "$APP"
if (( LAUNCH )); then
  echo "[$(date +%T)] launching"
  xcrun devicectl device process launch --device "$DEVICE" --terminate-existing dk.dorup.skycam
fi
echo "[$(date +%T)] done"
