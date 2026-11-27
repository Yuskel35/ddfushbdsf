"""Panel web para administrar un servidor Mohist desde GitHub Codespaces."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


API = "https://api.mohistmc.com/project/mohist"
PORT = int(os.environ.get("PORT", "8000"))
USER_AGENT = "Mohist-Codespaces-Launcher/1.0"


class ServerManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.downloading = False
        self.logs: deque[str] = deque(maxlen=1500)
        self.next_log_id = 0
        self.metrics_lock = threading.Lock()
        self.last_cpu_sample = None
        self.last_process_sample = None

    def log(self, message: str):
        with self.lock:
            self.next_log_id += 1
            self.logs.append(f"{self.next_log_id}\t{message.rstrip()}")

    def output_reader(self, process: subprocess.Popen):
        assert process.stdout is not None
        for line in process.stdout:
            self.log(line)
        code = process.wait()
        self.log(f"Servidor detenido (código {code}).")

    def state(self):
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {"running": running, "downloading": self.downloading}

    def metrics(self):
        result = {
            "cpu_percent": None,
            "ram_used_bytes": None,
            "ram_total_bytes": None,
            "server_ram_bytes": None,
            "disk_used_bytes": None,
            "disk_total_bytes": None,
        }

        # Prefer the Codespace container limit when cgroups expose one.
        cgroup_paths = [
            (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory.max")),
            (Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")),
        ]
        for used_path, total_path in cgroup_paths:
            try:
                used_text = used_path.read_text().strip()
                total_text = total_path.read_text().strip()
                if total_text != "max":
                    used, total = int(used_text), int(total_text)
                    if 0 < total < (1 << 60):
                        result["ram_used_bytes"] = used
                        result["ram_total_bytes"] = total
                        break
            except (OSError, ValueError):
                continue

        if result["ram_total_bytes"] is None:
            try:
                memory = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, value = line.split(":", 1)
                    memory[key] = int(value.strip().split()[0]) * 1024
                total = memory.get("MemTotal")
                available = memory.get("MemAvailable")
                if total and available is not None:
                    result["ram_total_bytes"] = total
                    result["ram_used_bytes"] = total - available
            except (OSError, ValueError, IndexError):
                pass

        try:
            disk = shutil.disk_usage(Path.cwd())
            result["disk_used_bytes"] = disk.used
            result["disk_total_bytes"] = disk.total
        except OSError:
            pass

        now = time.monotonic()
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            counters = [int(value) for value in fields]
            total_ticks = sum(counters)
            idle_ticks = counters[3] + (counters[4] if len(counters) > 4 else 0)
            with self.metrics_lock:
                previous = self.last_cpu_sample
                self.last_cpu_sample = (now, total_ticks, idle_ticks)
            if previous:
                elapsed = now - previous[0]
                total_delta = total_ticks - previous[1]
                idle_delta = idle_ticks - previous[2]
                if elapsed > 0 and total_delta > 0:
                    result["cpu_percent"] = round(100 * (1 - idle_delta / total_delta), 1)
        except (OSError, ValueError, IndexError):
            pass

        with self.lock:
            process = self.process
            pid = process.pid if process and process.poll() is None else None
        if pid:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
                values = stat[stat.rfind(")") + 2:].split()
                process_ticks = int(values[11]) + int(values[12])
                resident_pages = int(values[21])
                result["server_ram_bytes"] = resident_pages * os.sysconf("SC_PAGE_SIZE")
                with self.metrics_lock:
                    previous = self.last_process_sample
                    self.last_process_sample = (pid, now, process_ticks)
                if previous and previous[0] == pid:
                    elapsed = now - previous[1]
                    if elapsed > 0:
                        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                        cores = max(1, os.cpu_count() or 1)
                        result["server_cpu_percent"] = round(
                            100 * (process_ticks - previous[2]) / ticks_per_second / elapsed / cores, 1
                        )
                else:
                    result["server_cpu_percent"] = None
            except (OSError, ValueError, IndexError, KeyError):
                pass
        else:
            with self.metrics_lock:
                self.last_process_sample = None

        result.setdefault("server_cpu_percent", None)
        return result


manager = ServerManager()


PAGE = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Launcher Mohist · Codespaces</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#121a2b;--line:#29354a;--muted:#aab7cb;--accent:#7c9cff;--green:#57d6a0}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 80% 0,#1a2850 0,transparent 42%),var(--bg);color:#edf2fb;font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:970px;margin:36px auto;padding:0 18px}.hero{margin-bottom:20px}.tag{color:#a8bcff;font-size:12px;letter-spacing:.12em;text-transform:uppercase}.hero h1{margin:6px 0;font-size:30px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:linear-gradient(145deg,#151e31,#101727);border:1px solid var(--line);border-radius:14px;padding:18px}.wide{grid-column:1/-1}h2{font-size:17px;margin:0 0 14px}label{display:block;font-size:13px;color:#bfcae0;margin:12px 0 6px}input,select{width:100%;background:#0b1220;color:#eef3ff;border:1px solid #34425b;border-radius:8px;padding:10px 11px;font:inherit}input[type=checkbox]{width:auto;accent-color:var(--accent)}button{border:0;border-radius:8px;padding:10px 14px;background:var(--accent);color:#071024;font:600 14px system-ui;cursor:pointer}button.secondary{background:#26344d;color:#edf2fb}button.danger{background:#662f3c;color:#fff}button:disabled{opacity:.45;cursor:wait}.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:14px}.check{display:flex;gap:9px;align-items:flex-start;color:#c2cde0;font-size:13px;margin-top:14px}.notice{border-left:3px solid #e4b65b;padding:9px 12px;background:#262113;color:#ead9ae;border-radius:4px;font-size:13px;margin-top:12px}.status{display:inline-flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}.dot{height:9px;width:9px;border-radius:50%;background:#74819a}.dot.on{background:var(--green);box-shadow:0 0 12px #57d6a088}pre{height:300px;overflow:auto;background:#080d17;border:1px solid var(--line);border-radius:8px;padding:12px;color:#d0dded;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap}.command{display:flex;gap:8px}.command input{flex:1}.small{font-size:12px}.ports{margin-top:14px}a{color:#a8bcff}
#op_name{max-width:320px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:0 0 16px}.metric{background:#111a2b;border:1px solid var(--line);border-radius:10px;padding:12px}.metric-name{display:block;color:var(--muted);font-size:12px}.metric-value{display:block;font-size:18px;font-weight:650;margin-top:3px}.metric-detail{display:block;color:var(--muted);font-size:11px;margin-top:2px}.meter{height:5px;background:#27334a;border-radius:5px;margin-top:9px;overflow:hidden}.meter i{display:block;height:100%;width:0;background:var(--accent);border-radius:5px;transition:width .35s}
@media(max-width:720px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}main{margin:20px auto}.hero h1{font-size:25px}}
</style></head>
<body><main>
<header class="hero"><div class="tag">Panel local · GitHub Codespaces</div><h1>Servidor Minecraft con Mohist</h1><div class="muted">Crea y controla un servidor Forge + Bukkit/Spigot desde el navegador.</div></header>
<section class="metrics" aria-label="Uso de recursos">
<div class="metric"><span class="metric-name">CPU del Codespace</span><strong class="metric-value" id="cpu_value">Leyendo…</strong><div class="meter"><i id="cpu_meter"></i></div></div>
<div class="metric"><span class="metric-name">RAM del Codespace</span><strong class="metric-value" id="ram_value">Leyendo…</strong><span class="metric-detail" id="ram_detail"></span><div class="meter"><i id="ram_meter"></i></div></div>
<div class="metric"><span class="metric-name">CPU del servidor Java</span><strong class="metric-value" id="server_cpu_value">—</strong><span class="metric-detail">Porcentaje de los CPU del Codespace</span><div class="meter"><i id="server_cpu_meter"></i></div></div>
<div class="metric"><span class="metric-name">RAM del servidor Java</span><strong class="metric-value" id="server_ram_value">—</strong><span class="metric-detail">Memoria residente del proceso</span></div>
<div class="metric"><span class="metric-name">Almacenamiento</span><strong class="metric-value" id="disk_value">Leyendo…</strong><span class="metric-detail" id="disk_detail"></span><div class="meter"><i id="disk_meter"></i></div></div>
</section>
<div class="grid">
<section class="card"><h2>1. Configuración</h2>
<label for="version">Versión de Minecraft</label><select id="version"><option>1.20.1</option></select>
<label for="directory">Carpeta del servidor (dentro del Codespace)</label><input id="directory" value="server" placeholder="server">
<label for="ram">RAM máxima (GB)</label><input id="ram" type="number" min="1" max="64" value="4">
<div class="row"><button id="download" onclick="downloadServer()">Descargar Mohist</button><span class="status" id="status"><i class="dot"></i> Listo</span></div>
<p class="muted small">Se descarga el build más reciente desde la API oficial de MohistMC.</p></section>
<section class="card"><h2>2. Iniciar y aceptar la EULA</h2>
<div class="check"><input id="eula" type="checkbox"><span>Leí y acepto la <a href="https://www.minecraft.net/eula" target="_blank" rel="noreferrer">EULA oficial de Minecraft</a>.</span></div>
<div class="notice">La primera vez, inicia sin marcar la casilla para que Mohist genere <code>eula.txt</code>. Léelo; si aceptas, marca la casilla y vuelve a iniciar.</div>
<div class="row"><button id="start" onclick="startServer()">Iniciar servidor</button><button id="stop" class="danger" onclick="stopServer()" disabled>Detener</button></div>
<div class="ports muted small">Para que entren jugadores, expón también el puerto <b>25565</b> en la pestaña <b>Ports</b> de Codespaces.</div></section>
<section class="card wide"><h2>Consola</h2><pre id="logs" aria-live="polite">Conectando con el panel…</pre>
<form class="command" onsubmit="sendCommand(event)"><input id="cmd" placeholder="Comando del servidor, por ejemplo: list" autocomplete="off"><button class="secondary">Enviar</button></form>
<div class="row"><input id="op_name" maxlength="16" placeholder="Nombre exacto del jugador" autocomplete="off"><button class="secondary" type="button" onclick="playerAction('op')">Dar OP</button><button class="secondary" type="button" onclick="playerAction('deop')">Quitar OP</button><button class="secondary" type="button" onclick="playerAction('ban')">Banear</button><button class="secondary" type="button" onclick="playerAction('pardon')">Quitar ban</button></div>
<p class="muted small">Después del primer inicio, coloca mods en <code>mods/</code> y plugins en <code>plugins/</code> dentro de la carpeta del servidor.</p></section>
</div></main>
<script>
let lastId=0;
const $=id=>document.getElementById(id);
function payload(){return {version:$('version').value,directory:$('directory').value,ram:$('ram').value,eula:$('eula').checked}}
async function post(path,data={}){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const j=await r.json();if(!r.ok)throw Error(j.error||'Error');return j}
function status(text,on=false){$('status').innerHTML=`<i class="dot ${on?'on':''}"></i> ${text}`}
function bytes(n){if(n===null||n===undefined)return '—';const units=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<units.length-1){n/=1024;i++}return `${n.toFixed(i===0?0:1)} ${units[i]}`}
function meter(id,pct){$(id).style.width=`${Math.max(0,Math.min(100,pct||0))}%`}
async function loadMetrics(){try{const m=await(await fetch('/api/metrics',{cache:'no-store'})).json();$('cpu_value').textContent=m.cpu_percent===null?'—':`${m.cpu_percent}%`;meter('cpu_meter',m.cpu_percent);$('ram_value').textContent=bytes(m.ram_used_bytes);$('ram_detail').textContent=m.ram_total_bytes?`de ${bytes(m.ram_total_bytes)}`:'';meter('ram_meter',m.ram_used_bytes&&m.ram_total_bytes?100*m.ram_used_bytes/m.ram_total_bytes:0);$('server_cpu_value').textContent=m.server_cpu_percent===null?'—':`${m.server_cpu_percent}%`;meter('server_cpu_meter',m.server_cpu_percent);$('server_ram_value').textContent=bytes(m.server_ram_bytes);$('disk_value').textContent=bytes(m.disk_used_bytes);$('disk_detail').textContent=m.disk_total_bytes?`de ${bytes(m.disk_total_bytes)} usados`:'';meter('disk_meter',m.disk_used_bytes&&m.disk_total_bytes?100*m.disk_used_bytes/m.disk_total_bytes:0)}catch(e){}}
async function loadVersions(){try{const r=await fetch('/api/versions');const j=await r.json();if(j.versions?.length){$('version').innerHTML=j.versions.map(v=>`<option>${v}</option>`).join('');if(j.versions.includes('1.20.1'))$('version').value='1.20.1'}}catch(e){}}
async function refresh(){try{const s=await(await fetch('/api/state')).json();$('start').disabled=s.running||s.downloading;$('download').disabled=s.running||s.downloading;$('stop').disabled=!s.running;if(s.running)status('Servidor en ejecución',true);else if(s.downloading)status('Descargando…');else status('Detenido');const r=await fetch('/api/logs?after='+lastId);const j=await r.json();if(j.lines.length){const box=$('logs');box.textContent+=(box.textContent?'\n':'')+j.lines.map(x=>{const i=x.indexOf('\t');lastId=Math.max(lastId,Number(x.slice(0,i)));return x.slice(i+1)}).join('\n');box.scrollTop=box.scrollHeight}}catch(e){status('Reconectando…')}setTimeout(refresh,1200)}
async function downloadServer(){try{status('Descargando…');await post('/api/download',payload());status('Descarga iniciada')}catch(e){alert(e.message)}}
async function startServer(){try{await post('/api/start',payload())}catch(e){alert(e.message)}}
async function stopServer(){try{await post('/api/stop')}catch(e){alert(e.message)}}
async function playerAction(action){const player=$('op_name').value.trim();if(!/^[A-Za-z0-9_]{3,16}$/.test(player)){alert('Escribe un nombre válido de Minecraft (3–16 letras, números o guion bajo).');return}if(action==='ban'&&!confirm(`¿Banear a ${player}?`))return;try{await post('/api/player-action',{player,action})}catch(e){alert(e.message)}}
async function sendCommand(e){e.preventDefault();const command=$('cmd').value.trim();if(!command)return;try{await post('/api/command',{command});$('cmd').value=''}catch(err){alert(err.message)}}
loadVersions();loadMetrics();setInterval(loadMetrics,2500);refresh();
</script></body></html>'''


def json_response(handler: BaseHTTPRequestHandler, status: int, data):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[panel] " + fmt % args)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.path == "/api/versions":
            try:
                request = urllib.request.Request(f"{API}/versions", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                versions = payload.get("versions", []) if isinstance(payload, dict) else payload
                versions = [str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in versions]
                json_response(self, 200, {"versions": versions})
            except Exception as exc:
                json_response(self, 200, {"versions": ["1.20.1"], "warning": str(exc)})
        elif path.path == "/api/state":
            json_response(self, 200, manager.state())
        elif path.path == "/api/metrics":
            json_response(self, 200, manager.metrics())
        elif path.path == "/api/logs":
            try:
                after = int(dict(part.split("=", 1) for part in path.query.split("&") if "=" in part).get("after", "0"))
            except ValueError:
                after = 0
            with manager.lock:
                lines = [line for line in manager.logs if int(line.split("\t", 1)[0]) > after]
            json_response(self, 200, {"lines": lines})
        else:
            json_response(self, 404, {"error": "Ruta no encontrada"})

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("Solicitud demasiado grande")
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Datos JSON no válidos") from exc

    def do_POST(self):
        try:
            data = self.read_json()
            route = urlparse(self.path).path
            if route == "/api/download":
                self.download(data)
            elif route == "/api/start":
                self.start_server(data)
            elif route == "/api/stop":
                self.stop_server()
            elif route == "/api/command":
                self.command(data)
            elif route == "/api/player-action":
                self.player_action(data)
            else:
                json_response(self, 404, {"error": "Ruta no encontrada"})
        except ValueError as exc:
            json_response(self, 400, {"error": str(exc)})

    def settings(self, data):
        version = str(data.get("version", "")).strip()
        if not re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
            raise ValueError("Versión de Minecraft no válida")
        directory = Path(str(data.get("directory", "server"))).expanduser()
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        directory = directory.resolve()
        try:
            ram = int(data.get("ram", 4))
        except (TypeError, ValueError) as exc:
            raise ValueError("RAM debe ser un número entero") from exc
        if not 1 <= ram <= 64:
            raise ValueError("RAM debe estar entre 1 y 64 GB")
        return version, directory, ram

    def download(self, data):
        version, directory, _ = self.settings(data)
        with manager.lock:
            if manager.downloading or (manager.process and manager.process.poll() is None):
                json_response(self, 409, {"error": "Detén el servidor antes de descargar."})
                return
            manager.downloading = True

        def worker():
            destination = directory / f"Mohist-{version}-server.jar"
            temporary = destination.with_suffix(".jar.part")
            try:
                directory.mkdir(parents=True, exist_ok=True)
                url = f"{API}/{version}/builds/latest/download"
                manager.log(f"Descargando Mohist {version} desde la API oficial…")
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
                if temporary.stat().st_size < 1_000_000:
                    raise RuntimeError("El archivo recibido no parece ser un servidor Mohist válido.")
                temporary.replace(destination)
                manager.log(f"Descarga terminada: {destination}")
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                manager.log(f"Error al descargar Mohist: {exc}")
            finally:
                with manager.lock:
                    manager.downloading = False

        threading.Thread(target=worker, daemon=True).start()
        json_response(self, 202, {"ok": True})

    def start_server(self, data):
        version, directory, ram = self.settings(data)
        with manager.lock:
            if manager.downloading:
                json_response(self, 409, {"error": "Espera a que termine la descarga."})
                return
            if manager.process and manager.process.poll() is None:
                json_response(self, 409, {"error": "El servidor ya está iniciado."})
                return
        if not shutil.which("java"):
            json_response(self, 400, {"error": "Java no está instalado en este Codespace."})
            return
        jar = directory / f"Mohist-{version}-server.jar"
        if not jar.is_file():
            json_response(self, 400, {"error": "Primero descarga Mohist para esta versión y carpeta."})
            return

        eula_file = directory / "eula.txt"
        accepted = bool(data.get("eula", False))
        if eula_file.exists():
            contents = eula_file.read_text(encoding="utf-8", errors="replace")
            already_accepted = re.search(r"(?m)^\s*eula\s*=\s*true\s*$", contents, re.I)
            if not already_accepted and accepted:
                eula_file.write_text("# Aceptada desde el panel tras confirmación del usuario.\neula=true\n", encoding="utf-8")
            elif not already_accepted:
                manager.log("Primera ejecución: lee eula.txt. Si aceptas la EULA, marca la casilla y vuelve a iniciar.")
        elif accepted:
            eula_file.write_text("# Aceptada desde el panel tras confirmación del usuario.\neula=true\n", encoding="utf-8")

        try:
            process = subprocess.Popen(
                ["java", "-Xms1G", f"-Xmx{ram}G", "-jar", jar.name, "nogui"],
                cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            json_response(self, 500, {"error": str(exc)})
            return
        with manager.lock:
            manager.process = process
        manager.log(f"Iniciando servidor {version} con {ram} GB de RAM…")
        threading.Thread(target=manager.output_reader, args=(process,), daemon=True).start()
        json_response(self, 202, {"ok": True})

    def stop_server(self):
        with manager.lock:
            process = manager.process
            if not process or process.poll() is not None:
                json_response(self, 409, {"error": "El servidor no está iniciado."})
                return
            if process.stdin:
                process.stdin.write("stop\n")
                process.stdin.flush()
        json_response(self, 200, {"ok": True})

    def command(self, data):
        command = str(data.get("command", "")).strip()
        if not command or len(command) > 500 or "\n" in command or "\r" in command:
            json_response(self, 400, {"error": "Comando vacío o no válido."})
            return
        with manager.lock:
            process = manager.process
            if not process or process.poll() is not None or not process.stdin:
                json_response(self, 409, {"error": "El servidor no está iniciado."})
                return
            process.stdin.write(command + "\n")
            process.stdin.flush()
        json_response(self, 200, {"ok": True})

    def player_action(self, data):
        player = str(data.get("player", "")).strip()
        action = str(data.get("action", "")).strip()
        commands = {"op": "op", "deop": "deop", "ban": "ban", "pardon": "pardon"}
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", player):
            json_response(self, 400, {"error": "Nombre no válido. Usa entre 3 y 16 letras, números o guion bajo."})
            return
        if action not in commands:
            json_response(self, 400, {"error": "Acción no válida."})
            return
        with manager.lock:
            process = manager.process
            if not process or process.poll() is not None or not process.stdin:
                json_response(self, 409, {"error": "Inicia el servidor para usar los botones de moderación."})
                return
            process.stdin.write(f"{commands[action]} {player}\n")
            process.stdin.flush()
        manager.log(f"Comando {commands[action]} enviado para {player}.")
        json_response(self, 200, {"ok": True})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Panel listo en 0.0.0.0:{PORT}. En Codespaces, abre el puerto {PORT} en Ports.", flush=True)
    print("Minecraft usa el puerto 25565; reenvíalo también para conexiones de jugadores.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Cerrando el panel…")
    finally:
        with manager.lock:
            process = manager.process
        if process and process.poll() is None and process.stdin:
            process.stdin.write("stop\n")
            process.stdin.flush()
        server.server_close()


if __name__ == "__main__":
    main()
