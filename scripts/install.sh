#!/bin/bash

# Exit on error
set -e

# Default values
ANDROID_HOME="$HOME/android-sdk"
JDK_VERSION="17"
SDK_VERSION="34"
BUILD_TOOLS_VERSION="34.0.0"
NDK_VERSION="24.0.8215888"
APP_VERSION="2.2.1"
GRADLE_PROPERTIES="$HOME/.gradle/gradle.properties"

echo "=== Android SDK Installer for Linux ==="

# 0. Check for dependencies
for cmd in curl unzip; do
    if ! command -v $cmd &> /dev/null; then
        echo "Error: $cmd is not installed. Please install it first (apt install $cmd)."
        exit 1
    fi
done

# 1. Install Java
echo "Checking for Java..."
if ! command -v java &> /dev/null; then
    echo "Java not found. Installing OpenJDK $JDK_VERSION..."
    apt update
    apt install -y "openjdk-$JDK_VERSION-jdk"
else
    echo "Java is already installed: $(java -version 2>&1 | head -n 1)"
fi

# 2. Create SDK directory
echo "Creating SDK directory at $ANDROID_HOME..."
mkdir -p "$ANDROID_HOME/cmdline-tools"

# 3. Download Command Line Tools
# Note: You might want to update this URL to the latest version from 
# https://developer.android.com/studio#command-line-tools-only
CMD_LINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip"
TMP_ZIP="/tmp/cmdline-tools.zip"

if [ ! -d "$ANDROID_HOME/cmdline-tools/latest" ]; then
    echo "Downloading Android Command Line Tools..."
    curl -o "$TMP_ZIP" "$CMD_LINE_TOOLS_URL"
    
    echo "Extracting tools..."
    unzip -q "$TMP_ZIP" -d "$ANDROID_HOME/cmdline-tools"
    mv "$ANDROID_HOME/cmdline-tools/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm "$TMP_ZIP"
else
    echo "Command line tools already present."
fi

# 4. Install SDK Components
echo "Installing SDK components: platform-tools, android-$SDK_VERSION, build-tools, ndk..."
# Accept licenses automatically
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" --licenses
yes | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$ANDROID_HOME" "platform-tools" "platforms;android-$SDK_VERSION" "build-tools;$BUILD_TOOLS_VERSION" "ndk;$NDK_VERSION"

# 5. Configure Gradle and Environment Variables
echo "Writing Gradle properties..."
mkdir -p "$(dirname "$GRADLE_PROPERTIES")"
cat > "$GRADLE_PROPERTIES" <<EOF
version=$APP_VERSION
NDK_VERSION=$NDK_VERSION
android.ndkVersion=$NDK_VERSION
org.gradle.jvmargs=-Xmx2048m -Dfile.encoding\\=UTF-8
android.injected.testOnly=false
android.native.buildOutput=verbose
android.useAndroidX=true
defaultAbiFilters=arm64-v8a
enable_develop_server=false
android.defaults.buildfeatures.buildconfig=false
android.nonTransitiveRClass=true
android.nonFinalResIds=false
EOF

echo "Updating shell profile..."

ENV_VARS="
# Android SDK
export ANDROID_HOME=\"$ANDROID_HOME\"
export ANDROID_NDK_HOME=\"$ANDROID_HOME/ndk/$NDK_VERSION\"
export PATH=\"\$PATH:\$ANDROID_HOME/cmdline-tools/latest/bin\"
export PATH=\"\$PATH:\$ANDROID_HOME/platform-tools\"
export PATH=\"\$PATH:\$ANDROID_HOME/emulator\"
export PATH=\"\$PATH:\$ANDROID_HOME/ndk/$NDK_VERSION\"
"

# Determine which profile to update
if [ -f "$HOME/.zshrc" ]; then
    PROFILE="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    PROFILE="$HOME/.bashrc"
else
    PROFILE="$HOME/.profile"
fi

if ! grep -q "ANDROID_HOME" "$PROFILE"; then
    echo "$ENV_VARS" >> "$PROFILE"
    echo "Environment variables added to $PROFILE"
else
    echo "Environment variables already exist in $PROFILE"
fi

# 6. Final Verification
echo "Verifying installation..."
export ANDROID_HOME="$ANDROID_HOME"
export PATH="$PATH:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools"

if command -v adb &> /dev/null; then
    echo "Success! adb is working: $(adb --version | head -n 1)"
else
    echo "Note: adb not yet in current path. It will be available after you run 'source $PROFILE'."
fi

echo "=== Installation Complete ==="
echo "Please run 'source $PROFILE' or restart your terminal to apply changes."
echo ""
echo "NEXT STEPS:"
echo "1. Verify with: adb version"
echo "2. Install an Emulator: See the 'Emulator Setup' section in README.md"
