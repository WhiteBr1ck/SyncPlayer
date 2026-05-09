# SyncPlayer

SyncPlayer is a portable Windows launcher for synchronized mpv playback across local displays and LAN-connected computers.

## Features

- Open one video file in one or more mpv windows.
- Synchronize play, pause, seek, fullscreen, windowed mode, and close events.
- Use local multi-screen sync on one computer.
- Use LAN sync between multiple computers running SyncPlayer.
- Run as a LAN host or connect as a LAN client.
- Test LAN host connectivity before playback.
- Use existing mpv portable configuration without overriding rendering settings.
- Optional playback settings for fullscreen, follower mute, subtitles, resume mode, and hardware decoding.

## Requirements

- Windows
- Python 3.13 or newer for source runs
- Dependencies from `requirements.txt`
- Bundled `mpv` folder next to `syncplayer.py` or `SyncPlayer.exe`

## Run From Source

```bat
python -m pip install -r requirements.txt
python syncplayer.py
```

Open a file directly:

```bat
python syncplayer.py "D:\path\to\video.mp4"
```

## Build

```bat
build.bat
```

The build creates:

```text
dist\SyncPlayer.exe
```

Keep the `mpv` folder next to the executable when distributing.

## LAN Sync

On the host computer:

- Enable LAN multi-screen sync.
- Select host mode.
- Set the listen address and port.
- Save LAN settings.

On each client computer:

- Enable LAN multi-screen sync.
- Select client mode.
- Enter the host IP address and port.
- Test the connection.
- Save LAN settings.

All computers must be able to access the same video file path.

## Local Config

User settings are stored in:

```text
syncplayer.json
```

This file is local user state and is not intended to be committed.
