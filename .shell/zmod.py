# (C) 2024-2026 ghzserg https://github.com/ghzserg/zmod
import os
import logging
import json
import hashlib
import threading
from urllib.parse import quote
from urllib.request import Request, urlopen
from zipfile import ZipFile
from pathlib import Path
import re

POLY_RE = re.compile(r'POLYGON=\[\[(.*?)\]\]')
NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')

FFCONFIG1='/usr/prog/config/Adventurer5M.json'
FFCONFIG2='/opt/config/Adventurer5M.json'

class zmod:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.lang = config.get('language', 'en')
        self.ad5x = config.getboolean('ad5x', False)
        self.c5pro = config.getboolean('c5pro', False)
        self.screen = config.getboolean('screen', True)

        self.gcode = self.printer.lookup_object('gcode')
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.on_error_gcode = gcode_macro.load_template(
            config, 'on_error_gcode', '')

        self.virtual_sdcard = None
        self.sdcard_dirname = ""

        self.gcode.register_command('SAVE_SHAPER', self.cmd_SAVE_SHAPER)
        self.gcode.register_command('ZEXCLUDE', self.cmd_ZEXCLUDE)
        self.gcode.register_command('LOAD_ZOFFSET_NATIVE', self.cmd_LOAD_ZOFFSET_NATIVE)
        self.gcode.register_command('SDCARD_PRINT_FILE_MF', self.cmd_SDCARD_PRINT_FILE_MF)
        self.gcode.register_command('ZEXCLUDE_MF', self.cmd_ZEXCLUDE_MF)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
        self.sdcard_dirname = self.virtual_sdcard.sdcard_dirname

    def cmd_SDCARD_PRINT_FILE_MF(self, gcmd):
        filename = gcmd.get("FILENAME", None)
        if not filename:
            gcmd.respond_raw("SDCARD_PRINT_FILE_3MF: FILENAME is required (no file loaded)")
            return
        fname = self._extract_3mf_gcode(filename, gcmd)
        self.gcode.run_script_from_command(f"SDCARD_PRINT_FILE FILENAME=\"{fname}\"")

    def update_metadata_async(self, filename):
        """Запускает повторное сканирование метаданных в Moonraker, не блокируя реактор."""
        encoded = quote(filename, safe="")
        url = "http://127.0.0.1:7125/server/files/metascan?filename=" + encoded

        def _worker():
            req = Request(url, method="POST")
            try:
                with urlopen(req, timeout=30):
                    pass
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _extract_3mf_gcode(self, fname, gcmd):
        if not fname.lower().endswith('.3mf'):
            return fname

        fname = fname.lstrip('/')
        src_3mf_path = os.path.join(self.sdcard_dirname, fname)
        p = Path(fname)
        clean_name = p.stem if p.stem.lower().endswith('.gcode') else f"{p.stem}.gcode"

        # Сохраняем подпапки исходного пути внутри 3mf_print
        rel_dir = p.parent.as_posix()
        if rel_dir in ('', '.'):
            rel_gcode = f"3mf_print/{clean_name}"
        else:
            rel_gcode = f"3mf_print/{rel_dir}/{clean_name}"

        target_gcode_path = os.path.join(self.sdcard_dirname, rel_gcode)

        real_root = os.path.realpath(self.sdcard_dirname)
        if not os.path.realpath(target_gcode_path).startswith(real_root + os.sep):
            raise gcmd.error("3MF: invalid path")

        os.makedirs(os.path.dirname(target_gcode_path), exist_ok=True)

        try:
            with ZipFile(src_3mf_path, 'r') as zip_ref:
                file_list = zip_ref.namelist()

                internal_gcode_names = [f for f in file_list if f.lower().endswith('.gcode')]
                if not internal_gcode_names:
                    raise ValueError("NO_GCODE")

                internal_gcode_names.sort()
                gcode_in_zip = internal_gcode_names[0]

                expected_md5 = None
                md5_file_path = gcode_in_zip + ".md5"
                if md5_file_path in file_list:
                    raw_md5_content = zip_ref.read(md5_file_path).decode('utf-8', errors='ignore').strip()
                    if raw_md5_content:
                        expected_md5 = raw_md5_content.split()[0].lower()

                # Потоковое чтение и запись чанками по 16КБ для экономии ОЗУ
                md5_hash = hashlib.md5()
                with zip_ref.open(gcode_in_zip) as source, open(target_gcode_path, 'wb') as target:
                    while True:
                        chunk = source.read(16384)
                        if not chunk:
                            break
                        target.write(chunk)
                        md5_hash.update(chunk)

                current_md5 = md5_hash.hexdigest().lower()

                # Если хеш не совпал с тем, что в архиве — удаляем битый файл с диска
                if expected_md5 and current_md5 != expected_md5:
                    if os.path.exists(target_gcode_path):
                        os.remove(target_gcode_path)
                    raise ValueError("MD5_MISMATCH")

                self.update_metadata_async(rel_gcode)

            return rel_gcode
        except Exception as e:
            if str(e) == "NO_GCODE":
                err = "!! Ошибка 3MF: Внутри архива не найден файл G-кода" if self.lang == 'ru' else "!! 3MF Error: No G-code file found inside the archive"
            elif str(e) == "MD5_MISMATCH":
                err = "!! Ошибка MD5: Контрольная сумма gcode не совпадает с файлом .md5 в 3MF" if self.lang == 'ru' else "!! MD5 Error: Extracted G-code hash does not match the internal .md5 file"
            else:
                err = f"!! Ошибка распаковки архива 3MF: {str(e)}" if self.lang == 'ru' else f"!! 3MF unpack error: {str(e)}"
            raise gcmd.error(err)

    def cmd_ZEXCLUDE_MF(self, gcmd):
        filename = gcmd.get("FILENAME", None)
        if not filename:
            gcmd.respond_raw("ZEXCLUDE_MF: FILENAME is required")
            return

        if filename.startswith('/'):
            filename = filename[1:]
        full_path = os.path.join(self.sdcard_dirname, filename)

        if not full_path.lower().endswith('.3mf'):
            gcmd.respond_raw("ZEXCLUDE_MF: File must be a .3mf archive")
            return

        try:
            with ZipFile(full_path, 'r') as zip_ref:
                file_list = zip_ref.namelist()

                config_file = 'Metadata/plate_1.json'
                if config_file not in file_list:
                    gcmd.respond_raw(f"ZEXCLUDE_MF: Configuration file '{config_file}' not found inside 3MF")
                    return

                raw_content = zip_ref.read(config_file).decode('utf-8', errors='ignore')
                data = json.loads(raw_content)

                bbox_all = data.get("bbox_all", None)
                if not bbox_all or len(bbox_all) != 4:
                    gcmd.respond_raw("ZEXCLUDE_MF: 'bbox_all' valid data not found in slice_info.config")
                    return

                x_min, y_min, x_max, y_max = float(bbox_all[0]), float(bbox_all[1]), float(bbox_all[2]), float(bbox_all[3])

                exclude_cmd = (
                    f"_SET_FAST_KAMP "
                    f"X_START={x_min:.4f} Y_START={y_min:.4f} "
                    f"X_END={x_max:.4f} Y_END={y_max:.4f}"
                )

                gcmd.respond_raw(f"ZEXCLUDE_MF: Defining SET_FAST_MAP from 3MF metadata")
                self.gcode.run_script_from_command(exclude_cmd)

        except FileNotFoundError:
            gcmd.respond_raw(f"ZEXCLUDE_MF: File not found: {full_path}")
            logging.error(f"ZEXCLUDE_MF: File not found: {full_path}")
        except json.JSONDecodeError:
            gcmd.respond_raw("ZEXCLUDE_MF: Failed to parse JSON in slice_info.config")
            logging.error("ZEXCLUDE_MF: JSON decode error")
        except Exception as e:
            logging.exception("zexclude_mf_error")
            gcmd.respond_raw(f"ZEXCLUDE_MF: Unexpected error: {str(e)}")

    def get_lang(self):
        return self.lang

    def cmd_LOAD_ZOFFSET_NATIVE(self, gcmd):
        start = gcmd.get_int("START", 0)
        if self.c5pro:
            return

    def cmd_ZEXCLUDE(self, gcmd):
        filename = gcmd.get("FILENAME", None)
        if not filename:
            file_path = self.virtual_sdcard.file_path()
            if not file_path:
                gcmd.respond_raw("ZEXCLUDE: FILENAME is required (no file loaded)")
                return
            full_path = file_path
        else:
            if filename.startswith('/'):
                filename = filename[1:]
            full_path = os.path.join(self.sdcard_dirname, filename)

        x_min = y_min = float('inf')
        x_max = y_max = float('-inf')
        found = False

        try:
            with open(full_path, 'r') as f:
                gcmd.respond_raw(f"ZEXCLUDE: {full_path}")

                max_lines = 3000
                line_num = 0
                for line in f:
                    line_num += 1
                    if line_num > max_lines:
                        break

                    if not line.startswith('EXCLUDE_OBJECT_DEFINE'):
                        continue

                    m = POLY_RE.search(line)
                    if not m:
                        continue

                    nums = [float(n) for n in NUM_RE.findall(m.group(1))]
                    xs = nums[0::2]
                    ys = nums[1::2]
                    if xs and ys:
                        x_min = min(x_min, min(xs))
                        x_max = max(x_max, max(xs))
                        y_min = min(y_min, min(ys))
                        y_max = max(y_max, max(ys))
                        found = True

        except FileNotFoundError:
            gcmd.respond_raw(f"ZEXCLUDE: File not found: {full_path}")
            logging.error(f"ZEXCLUDE: File not found: {full_path}")
            return
        except Exception:
            logging.exception("zexclude_file_error")
            gcmd.respond_raw("ZEXCLUDE: Unexpected error during file processing")
            return

        try:
            if found and x_max > x_min and y_max > y_min:
                self.gcode.run_script_from_command(
                    f"_SET_FAST_KAMP X_START={x_min:.4f} Y_START={y_min:.4f} "
                    f"X_END={x_max:.4f} Y_END={y_max:.4f}")
                gcmd.respond_raw(
                    f"ZEXCLUDE: Fast KAMP area {x_min:.1f},{y_min:.1f} - {x_max:.1f},{y_max:.1f}")
            else:
                self.gcode.run_script_from_command("_RESET_FAST_KAMP")
                gcmd.respond_raw("ZEXCLUDE: No objects found, fast KAMP reset")
        except self.gcode.error as e:
            error_message = str(e)
            gcmd.respond_raw(f"ZEXCLUDE: Error: {error_message}")
            logging.error(f"ZEXCLUDE: Error: {error_message}")
            try:
                self.gcode.run_script_from_command(self.on_error_gcode.render())
                gcmd.respond_raw("ZEXCLUDE: Error handler executed")
            except:
                logging.exception("zexclude_error")
                gcmd.respond_raw("ZEXCLUDE: Error handler failed")
        except Exception:
            logging.exception("zexclude_error")
            gcmd.respond_raw("ZEXCLUDE: Unexpected error during script execution")

        gcmd.respond_raw("ZEXCLUDE: End")

    def cmd_SAVE_SHAPER(self, gcmd):
        shaper_name = gcmd.get('NAME', '').lower()
        frequency = gcmd.get_float('FREQUENCY', 0.0)
        shaper_axis = gcmd.get('AXIS', '').lower()

        # Валидация параметров
        valid_shapers = ['mzv', 'zv', 'zvd', 'ei', '2hump_ei', '3hump_ei']
        if shaper_name not in valid_shapers:
            if self.lang != 'ru':
                raise gcmd.error("Invalid shaper. Allowed: %s" % ', '.join(valid_shapers))
            else:
                raise gcmd.error("Неверный шейпер. Допустимые: %s" % ', '.join(valid_shapers))
        if frequency <= 0:
            if self.lang != 'ru':
                raise gcmd.error("Frequency must be > 0")
            else:
                raise gcmd.error("Частота должна быть > 0")
        if shaper_axis not in ['x', 'y']:
            if self.lang != 'ru':
                raise gcmd.error("Axis must be X or Y")
            else:
                raise gcmd.error("Ось должна быть X или Y")

        # Сохранение параметров через Klipper API
        configfile = self.printer.lookup_object('configfile')
        configfile.set('input_shaper', 'shaper_type_%s' % shaper_axis, shaper_name)
        configfile.set('input_shaper', 'shaper_freq_%s' % shaper_axis, "%.1f" % frequency)

        # Информационное сообщение
        if self.lang != 'ru':
            gcmd.respond_info(
                "Parameters for axis %s have been saved. Run SAVE_CONFIG to apply changes." %
                shaper_axis.upper()
            )
        else:
            gcmd.respond_info(
                "Параметры для оси %s сохранены. Для применения выполните SAVE_CONFIG" %
                shaper_axis.upper()
            )

def load_config(config):
    return zmod(config)
