[app]
title = RTSP Camera
package.name = rtspcamera
package.domain = org.rtspcam

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 1.0

requirements = python3,kivy,pillow,android

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,ACCESS_WIFI_STATE,ACCESS_NETWORK_STATE

android.api = 33
android.minapi = 21
android.ndk = 25c
android.accept_sdk_license = True
android.arch = arm64-v8a

p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
