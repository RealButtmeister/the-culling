"""The Culling: create in-place dropouts while retaining the full stem timeline."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from culling_core import process_stems


BG = '#10171c'
PANEL = '#19242c'
LINE = '#2c3d47'
INK = '#e6eee9'
MUTED = '#9eb0b5'
GREEN = '#a2d7ae'
TEAL = '#88c5c8'
STEMS = (
    ('instruments', 'Instruments bus', 'Your combined melodic and harmonic parts'),
    ('kick', 'Kick', 'The separate kick stem'),
    ('808', '808', 'The separate 808 stem'),
    ('drums', 'Drums', 'The rest of your drums together'),
)
READY_MESSAGE = 'Ready — full-length stems, Preview.wav and an edit map are saved. Open the result folder to listen.'


def completion_summary(result_dir):
    """Read optional export details on the worker, without delaying the UI thread."""
    try:
        path = Path(result_dir) / 'Edit map.json'
        if not path.is_file() or path.stat().st_size > 2_000_000:
            return READY_MESSAGE
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(manifest, dict):
            return READY_MESSAGE
        source, output = manifest.get('source', {}), manifest.get('output', {})
        if not isinstance(source, dict) or not isinstance(output, dict):
            return READY_MESSAGE

        def number(value):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                return value
            return None

        def seconds(info):
            frames, rate = number(info.get('frames')), number(info.get('sample_rate'))
            if frames is not None and frames >= 0 and rate is not None and rate > 0:
                return frames / rate
            duration = number(info.get('seconds'))
            return duration if duration is not None and duration >= 0 else None

        def clock(duration):
            hundredths = round(duration * 100)
            minutes, remainder = divmod(hundredths, 6000)
            return f'{minutes}:{remainder / 100:05.2f}'

        pieces = []
        source_seconds, output_seconds = seconds(source), seconds(output)
        if source_seconds is not None and output_seconds is not None:
            same = source_seconds == output_seconds
            pieces.append(f'Source {clock(source_seconds)} → output {clock(output_seconds)}'
                          + (', same length.' if same else '; duration differs — see Edit map.json.'))
        parts_kept = number(output.get('actual_parts_keep_percent'))
        if parts_kept is not None and 0 <= parts_kept <= 100:
            pieces.append(f'{parts_kept:.1f}% of active parts kept.')
        movement = manifest.get('movement')
        if isinstance(movement, list):
            pieces.append(f'{len(movement)} edit' + ('.' if len(movement) == 1 else 's.'))
        return 'Ready — ' + ' '.join(pieces) if pieces else READY_MESSAGE
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return READY_MESSAGE


def documents_directory():
    """Locate Documents without creating a folder or relying on the EXE path."""
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                        ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

        folder_id = GUID.from_buffer_copy(uuid.UUID('FDD39AD0-238F-46AF-ADB4-6C85480369C7').bytes_le)
        shell = ctypes.WinDLL('shell32')
        ole = ctypes.WinDLL('ole32')
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                              wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        result = ctypes.c_void_p()
        if shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result)) == 0:
            try:
                return Path(ctypes.wstring_at(result))
            finally:
                ole.CoTaskMemFree(result)
    return Path.home() / 'Documents'


class CullingApp:
    def __init__(self, root):
        self.root = root
        root.title('The Culling')
        root.geometry('980x780')
        root.minsize(860, 740)
        root.configure(bg=BG)
        self.paths = {key: '' for key, _, _ in STEMS}
        self.path_labels = {key: tk.StringVar(value='No file selected') for key, _, _ in STEMS}
        self.bpm = tk.StringVar(value='')
        self.keep_percent = tk.StringVar(value='70')
        self.keep_label = tk.StringVar(value='Keep 70% of parts · full song length')
        self.intensity = tk.StringVar(value='Balanced')
        self.status = tk.StringVar(value='Choose your four stems and enter their original BPM.')
        self.output_root = documents_directory() / 'The Culling'
        self.seed = 2026
        self.busy = False
        self.latest = None
        self.events = queue.Queue()
        self.frozen = []
        self._style()
        self._build()
        self.bpm.trace_add('write', lambda *_: self._update_ready())
        self.keep_percent.trace_add('write', lambda *_: self._update_keep_label())
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self._poll)

    def _style(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=INK, font=('Segoe UI', 10))
        style.configure('TFrame', background=BG)
        style.configure('Panel.TFrame', background=PANEL)
        style.configure('TLabel', background=BG, foreground=INK)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 29))
        style.configure('TButton', background=LINE, foreground=INK, padding=(14, 8), borderwidth=0)
        style.map('TButton', background=[('active', '#3a505b'), ('disabled', '#202d34')],
                  foreground=[('disabled', '#71838c')])
        style.configure('Go.TButton', background=GREEN, foreground='#11291b',
                        font=('Segoe UI Semibold', 13), padding=(29, 13))
        style.map('Go.TButton', background=[('active', '#c0ebc8'), ('disabled', '#304739')],
                  foreground=[('disabled', '#839e88')])
        style.configure('TEntry', fieldbackground=PANEL, foreground=INK, insertcolor=INK, padding=7)
        style.map('TEntry', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', INK)])
        style.configure('TSpinbox', fieldbackground=PANEL, background=LINE, foreground=INK,
                        insertcolor=INK, arrowcolor=INK, padding=7)
        style.configure('TCombobox', fieldbackground=PANEL, background=LINE, foreground=INK,
                        arrowcolor=INK, padding=7)
        style.map('TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', INK)])
        style.configure('Horizontal.TProgressbar', background=TEAL, troughcolor=PANEL,
                        bordercolor=PANEL, lightcolor=TEAL, darkcolor=TEAL)
        self.root.option_add('*TCombobox*Listbox.background', PANEL)
        self.root.option_add('*TCombobox*Listbox.foreground', INK)

    def _build(self):
        outer = ttk.Frame(self.root, padding=26)
        outer.pack(fill='both', expand=True)
        title_row = ttk.Frame(outer)
        title_row.pack(fill='x')
        ttk.Label(title_row, text='THE CULLING', style='Title.TLabel').pack(side='left')
        ttk.Label(title_row, text='2.0 · Full-length', style='Muted.TLabel').pack(side='left', padx=16, pady=(16, 0))
        ttk.Label(outer, text='Create space. Keep the whole song.', foreground=TEAL,
                  font=('Segoe UI', 12)).pack(anchor='w', pady=(2, 12))
        ttk.Label(outer, text='Gaps stay in place. The original song length and timing stay intact.',
                  style='Muted.TLabel').pack(anchor='w')

        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x', pady=(18, 0))
        self.progress = ttk.Progressbar(footer, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0, 12))
        actions = ttk.Frame(footer)
        actions.pack(fill='x')
        self.status_label = ttk.Label(actions, textvariable=self.status, style='Muted.TLabel', wraplength=565)
        self.status_label.pack(side='left', fill='x', expand=True, padx=(0, 18))
        self.generate_button = ttk.Button(actions, text='Cull song', style='Go.TButton',
                                         command=self.generate, state='disabled')
        self.generate_button.pack(side='right')
        self.open_button = ttk.Button(footer, text='Open result folder', command=self.open_result, state='disabled')
        self.open_button.pack(anchor='e', pady=(10, 0))

        inputs = ttk.Frame(outer)
        inputs.pack(fill='x', pady=(22, 0))
        inputs.columnconfigure(1, weight=1)
        for row, (key, title, detail) in enumerate(STEMS):
            title_frame = ttk.Frame(inputs)
            title_frame.grid(row=row, column=0, sticky='w', padx=(0, 16), pady=(0, 16))
            ttk.Label(title_frame, text=title, font=('Segoe UI Semibold', 11)).pack(anchor='w')
            ttk.Label(title_frame, text=detail, style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w')
            entry = ttk.Entry(inputs, textvariable=self.path_labels[key], state='readonly', width=24)
            entry.grid(row=row, column=1, sticky='ew', padx=(0, 10), pady=(0, 16))
            ttk.Button(inputs, text='Choose…', command=lambda stem=key: self.choose_file(stem)).grid(
                row=row, column=2, sticky='e', pady=(0, 16))
        ttk.Label(outer, text='WAV or FLAC · export all four from the same song start in 4/4.',
                  style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w')

        settings = ttk.Frame(outer)
        settings.pack(fill='x', pady=(23, 8))
        bpm_frame = ttk.Frame(settings)
        bpm_frame.grid(row=0, column=0, sticky='nw', padx=(0, 25))
        ttk.Label(bpm_frame, text='Original BPM', style='Muted.TLabel').pack(anchor='w', pady=(0, 5))
        self.bpm_entry = ttk.Entry(bpm_frame, textvariable=self.bpm, width=9)
        self.bpm_entry.pack(anchor='w')
        ttk.Label(bpm_frame, text='Enter your tempo', style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w', pady=(5, 0))
        keep_frame = ttk.Frame(settings)
        keep_frame.grid(row=0, column=1, sticky='nw', padx=(0, 25))
        ttk.Label(keep_frame, text='Parts to keep (%)', style='Muted.TLabel').pack(anchor='w', pady=(0, 5))
        self.keep_box = ttk.Spinbox(keep_frame, textvariable=self.keep_percent, from_=40, to=100, increment=5, width=8)
        self.keep_box.pack(anchor='w')
        ttk.Label(keep_frame, textvariable=self.keep_label, style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w', pady=(5, 0))
        movement_frame = ttk.Frame(settings)
        movement_frame.grid(row=0, column=2, sticky='nw', padx=(0, 25))
        ttk.Label(movement_frame, text='Movement', style='Muted.TLabel').pack(anchor='w', pady=(0, 5))
        self.intensity_box = ttk.Combobox(movement_frame, textvariable=self.intensity,
                                          values=('Subtle', 'Balanced', 'Bold'), state='readonly', width=13)
        self.intensity_box.pack(anchor='w')
        ttk.Button(settings, text='New variation', command=self.new_variation).grid(row=0, column=3, sticky='w', pady=(18, 0))

        ttk.Label(outer, text='An approximate target for active part time. At 100%, every part stays unchanged.',
                  style='Muted.TLabel', wraplength=760).pack(anchor='w', pady=(6, 0))
        ttk.Label(outer, text='Your originals stay safe. Results are saved as new WAV files in Documents / The Culling.',
                  style='Muted.TLabel', wraplength=760).pack(anchor='w', pady=(8, 0))

    def choose_file(self, key):
        if self.busy:
            return
        title = next(title for stem, title, _ in STEMS if stem == key)
        path = filedialog.askopenfilename(title='Choose ' + title,
                                          filetypes=[('Audio stems', '*.wav *.flac'), ('WAV', '*.wav'), ('FLAC', '*.flac')])
        if path:
            self.paths[key] = path
            self.path_labels[key].set(Path(path).name)
            self._update_ready()

    def _update_ready(self):
        if self.busy:
            return
        ready = all(self.paths.values()) and bool(self.bpm.get().strip())
        self.generate_button.configure(state='normal' if ready else 'disabled')

    def _update_keep_label(self):
        try:
            keep = int(self.keep_percent.get())
            text = f'Keep {keep}% of parts · full song length' if 40 <= keep <= 100 else 'Choose 40% to 100%'
        except ValueError:
            text = 'Choose 40% to 100%'
        self.keep_label.set(text)

    def new_variation(self):
        if self.busy:
            return
        self.seed += 1
        self.status.set('New variation ready. Cull again for different gaps across the full song.')

    def generate(self):
        if self.busy:
            return
        try:
            if not all(self.paths.values()):
                raise ValueError('Choose all four stems first: instruments, kick, 808 and drums.')
            for key, path in self.paths.items():
                if not Path(path).is_file():
                    raise ValueError(f'The {key} file is unavailable. Choose it again.')
            try:
                bpm = float(self.bpm.get().strip())
            except ValueError:
                raise ValueError('Enter the original song BPM before culling.') from None
            if not math.isfinite(bpm) or bpm <= 0:
                raise ValueError('BPM must be a positive number matching your song.')
            try:
                keep = int(self.keep_percent.get())
            except ValueError:
                raise ValueError('Choose a whole-number parts-to-keep amount from 40% to 100%.') from None
            if not 40 <= keep <= 100:
                raise ValueError('Choose a parts-to-keep amount from 40% to 100%.')
            intensity = self.intensity.get()
            if intensity not in ('Subtle', 'Balanced', 'Bold'):
                raise ValueError('Choose Subtle, Balanced or Bold movement.')
        except ValueError as exc:
            messagebox.showerror('Check your settings', str(exc), parent=self.root)
            return
        paths = dict(self.paths)
        seed, destination = self.seed, Path(self.output_root)
        self.busy = True
        self._freeze(True)
        self.status.set('Reading your stems…')
        self.progress.start(12)

        def worker():
            try:
                result = process_stems(paths=paths, bpm=bpm, keep_percent=keep, intensity=intensity,
                                       seed=seed, output_root=destination,
                                       progress=lambda text: self.events.put(('status', text)))
                self.events.put(('done', {'path': result, 'summary': completion_summary(result)}))
            except Exception as exc:
                self.events.put(('error', str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _freeze(self, freeze):
        if freeze:
            self.frozen = []

            def visit(widget):
                for child in widget.winfo_children():
                    if isinstance(child, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Spinbox)):
                        self.frozen.append((child, child.state()))
                        child.state(['disabled'])
                    visit(child)

            visit(self.root)
        else:
            for widget, state in self.frozen:
                if widget.winfo_exists():
                    widget.state(['!disabled'])
                    widget.state(state)
            self._update_ready()
            self.open_button.configure(state='normal' if self.latest else 'disabled')

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status':
                    self.status.set(value)
                    continue
                self.busy = False
                self.progress.stop()
                if kind == 'done':
                    result = value if isinstance(value, dict) else {'path': value}
                    self.latest = Path(result['path'])
                    self.status.set(result.get('summary') or READY_MESSAGE)
                else:
                    self.status.set('Culling stopped. ' + value)
                    messagebox.showerror('Culling stopped', value, parent=self.root)
                self._freeze(False)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def open_result(self):
        if not self.latest or self.busy:
            return
        try:
            os.startfile(self.latest)
        except OSError as exc:
            messagebox.showerror('Could not open the folder', str(exc), parent=self.root)

    def close(self):
        if self.busy:
            self.status.set('Culling is still running. This window can close when it finishes.')
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    CullingApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
