# The Culling

Create gaps and dropouts in four aligned stems while preserving the original song length and sample positions. Version 2.0 keeps the ending in place and exports four edited stems plus a listening preview.

## Run from source (Windows)

Install Python 3.13 with Tkinter and the Windows Python launcher, then run in this folder:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

After setup, `Start.bat` uses the local `.venv`. See [USER_GUIDE.md](USER_GUIDE.md) for controls and workflow.

## Source and distribution

This repository contains editable application source. Generated exports, private settings, API keys, user audio, bundled runtimes and packaged executables are excluded. Original local releases remain separate. No new license is granted for original application code in this publication; dependency notices retain their own terms.

## Optional Windows executable

From the configured environment, install PyInstaller and build:

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --onefile --windowed --name "The Culling" app.py
```

Review dependency redistribution notices before distributing a binary.
