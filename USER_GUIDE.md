# User guide

These notes describe the application. For this source checkout, use README.md and Start.bat instead of the original packaged launchers or executable.

```text
# The Culling 2.0 — full-length arrangement

The Culling makes space in your arrangement by dropping parts out and bringing
them back. Every stem keeps the original song length and sample positions.
Removed parts become gaps; later audio stays where it was. The ending stays in
place too.

Double-click **The Culling.exe**. The standalone Windows app includes everything
it needs. It processes locally and does not upload your audio.

## Prepare the four stems

Export WAV or FLAC files from the same start to the same end in 4/4:

- **Instruments bus** — your combined melodic and harmonic parts.
- **Kick** — the kick by itself.
- **808** — the bass by itself.
- **Drums** — the rest of your drums together.

Include the silent beginning and ending of individual parts. All four need the
same sample rate. Enter the original BPM; use a song with a constant tempo.
The combined drums bus is treated as one part, so its individual hats or snares
cannot be edited separately.

## Make a version

1. Choose the four stems and enter their original BPM.
2. Set **Parts to keep (%)**. Start at 70%. Lower values ask for more space;
   100% leaves the original parts in place. This never shortens the song.
3. Choose **Subtle**, **Balanced**, or **Bold** movement. Balanced creates clear
   musical dropouts and returns; Bold makes larger changes.
4. Press **Cull song**, then open the result folder.

The keep percentage is an approximate arrangement-density target, based on
active parts. Musical boundaries, existing silence, protected endings and
available backing can limit the amount removed. The result's edit map records
what was actually done. Short or sparse sources may allow fewer changes.

**New variation** changes the pattern used by the next run. Every completed run
saves to a fresh folder in **Documents / The Culling**. Original files stay safe.

## Use the result in FL Studio

The result contains **Instruments.wav**, **Kick.wav**, **808.wav**, **Drums.wav**,
and **Preview.wav**. All five files have the same frame count as the original
song. Import the four edited stems at the same position as your original stems;
they will stay aligned with your project and any other unprocessed tracks.

Preview.wav combines the stems with one constant listening gain. Use the four
separate stems for mixing. Their format and gain remain the same outside the
documented gaps and short fades at gap boundaries.

**Edit map.json** records the unchanged timeline, each dropout and the source
file hashes. **Read Me.txt** describes that particular result. An INCOMPLETE.txt
marker means an export has not finished successfully and should not be used.

Version 1 removed whole song sections and shortened the timeline. Version 2
changes the arrangement in place. Use fresh Version 2 exports for this behavior;
previous exports remain as they were.

## Source edition

Keep app.py, culling_core.py and Open The Culling.bat together. The source
edition needs Python with tkinter, numpy and soundfile. The standalone app
does not require a separate Python installation.
```
