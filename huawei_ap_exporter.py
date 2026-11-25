#!/usr/bin/env python3
"""Lê APs Huawei por SSH e grava métricas no formato texto do Prometheus.

O arquivo gerado é lido pelo textfile collector do node_exporter.
Suporta os dois CLIs que os APs Huawei usam:

  vrp    - CLI clássico (AP361, AP362, AP661...): "display radio all" etc.
  mdcli  - CLI novo em JSON (AP673, AP773...): "display wifi-radio/..." etc.
"""
import base64
import json
import os
import pty
import re
import select
import sys
import time
from pathlib import Path

CONFIG_FILE = os.environ.get("HUAWEI_AP_CONFIG", "/etc/huawei-ap/config.json")
PASS_FILE = os.environ.get("HUAWEI_AP_PASS_FILE", "/etc/huawei-ap/password")
OUT_FILE = os.environ.get("HUAWEI_AP_PROM_FILE", "/var/lib/prometheus/node-exporter/huawei-ap.prom")
KNOWN = os.environ.get("HUAWEI_AP_KNOWN_HOSTS", "/var/lib/huawei-ap/known_hosts")

BAND = {1: "2.4G", 2: "5G", 3: "6G"}
BW = {0: 20, 1: 40, 2: 80, 3: 160, 4: 320, 5: 320}
PHY = {1: "11a", 2: "11b", 4: "11n", 8: "11ac", 16: "11ac", 32: "11ax", 64: "11be"}
PROMPT = re.compile(r"^(?:<[^>\r\n]+>|\[[^\[\]\r\n]+\]|MDCLI>)\s*$")

VRP_COMMANDS = [
    "screen-length 0 temporary",
    "display version",
    "display radio all",
    "display station all",
    "display vap all",
    "display health",
    "display current-configuration | include led",
    "display current-configuration | include stelnet",
]
MDCLI_COMMANDS = [
    "display wifi-radio/wifi-radio-statistics/",
    "display wifi-sta/wifi-sta-statistics/",
    "display cpu-memory all",
    "display wlan-system-profile/system-profiles/",
    "display sshs/server-enable/",
]


def esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def label(pairs):
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in pairs if v is not None and v != "") + "}"


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Metrics:
    def __init__(self):
        self.lines = []
        self.seen = set()

    def add(self, name, help_text, labels, value, typ="gauge"):
        value = num(value)
        if value is None:
            return
        if name not in self.seen:
            self.lines.append(f"# HELP {name} {help_text}")
            self.lines.append(f"# TYPE {name} {typ}")
            self.seen.add(name)
        text = str(int(value)) if value.is_integer() else f"{value:g}"
        self.lines.append(f"{name}{label(labels)} {text}")

    def text(self):
        return "\n".join(self.lines) + "\n"


def run_ssh(host, user, password, commands, timeout=25):
    """Abre uma sessão interativa (os APs não aceitam comando remoto direto) e roda os comandos."""
    Path(KNOWN).parent.mkdir(parents=True, exist_ok=True)
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(
            "ssh",
            [
                "ssh", "-tt",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={KNOWN}",
                "-o", "PreferredAuthentications=keyboard-interactive,password",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "ConnectTimeout=8",
                f"{user}@{host}",
            ],
        )

    def read_for(seconds):
        end = time.time() + seconds
        data = b""
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], max(0.01, min(0.2, end - time.time())))
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
            if b"--More--" in chunk or b"---- More ----" in chunk:
                try:
                    os.write(fd, b" ")
                except OSError:
                    pass
        return data

    try:
        buf = b""
        start = time.time()
        while time.time() - start < 12 and b"password:" not in buf.lower():
            buf += read_for(0.4)
        if b"password:" not in buf.lower():
            raise RuntimeError(f"sem prompt de login em {host}")
        os.write(fd, password.encode() + b"\n")
        time.sleep(0.4)
        read_for(1.2)

        outputs = {}
        for cmd in commands:
            os.write(fd, cmd.encode() + b"\n")
            data = b""
            t0 = time.time()
            quiet = None
            answered = False
            while time.time() - t0 < timeout:
                chunk = read_for(0.25)
                if chunk:
                    data += chunk
                    quiet = None
                    low = data.lower()
                    if not answered and (b"[y/n]" in low or b"[y(yes)/n(no)" in low):
                        os.write(fd, b"n\n" if b"uncommitted" in low else b"y\n")
                        answered = True
                    continue
                if not data:
                    continue
                text = data.decode("utf-8", "replace").replace("\r", "")
                last = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")
                if PROMPT.match(last) or "Error:" in text or "Unrecognized" in text:
                    if quiet is None:
                        quiet = time.time()
                    elif time.time() - quiet > 0.35:
                        break
                else:
                    quiet = None
            outputs[cmd] = data.decode("utf-8", "replace").replace(password, "***")
        try:
            os.write(fd, b"quit\n")
        except OSError:
            pass
        return outputs
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        deadline = time.time() + 3
        try:
            while os.waitpid(pid, os.WNOHANG) == (0, 0):
                if time.time() > deadline:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.1)
        except ChildProcessError:
            pass


def json_after(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}


def b64_ip(value):
    try:
        raw = base64.b64decode(value)
    except Exception:
        return ""
    return ".".join(str(b) for b in raw) if len(raw) == 4 else ""


def base_labels(ap):
    return [("ap", ap["name"]), ("model", ap.get("model", "")), ("site", ap.get("site", "")), ("ip", ap["host"])]


def add_flags(metrics, base, led_on, ssh_on):
    metrics.add("huawei_ap_led_on", "1 se o LED do AP esta aceso", base, led_on)
    metrics.add("huawei_ap_ssh_up", "1 se o SSH do AP esta habilitado", base, ssh_on)


def add_uplink(metrics, base, iface, up, speed_mbps, rx_bytes, tx_bytes, rx_err, tx_err, crc):
    labels = base + [("interface", iface)]
    metrics.add("huawei_ap_uplink_up", "1 se a porta de uplink esta com link", labels, up)
    metrics.add("huawei_ap_uplink_speed_mbps", "Velocidade negociada da porta de uplink", labels, speed_mbps)
    metrics.add("huawei_ap_uplink_receive_bytes_total", "Bytes recebidos na porta de uplink", labels, rx_bytes, "counter")
    metrics.add("huawei_ap_uplink_transmit_bytes_total", "Bytes enviados na porta de uplink", labels, tx_bytes, "counter")
    metrics.add("huawei_ap_uplink_receive_errors_total", "Erros de recepcao na porta de uplink", labels, rx_err, "counter")
    metrics.add("huawei_ap_uplink_transmit_errors_total", "Erros de envio na porta de uplink", labels, tx_err, "counter")
    metrics.add("huawei_ap_uplink_crc_errors_total", "Erros de CRC na porta de uplink", labels, crc, "counter")


# ---------------------------------------------------------------- VRP


def parse_vrp(ap, out, metrics, extra):
    base = base_labels(ap)
    radio = out.get("display radio all", "")
    ok = "CH/BW" in radio
    metrics.add("huawei_ap_up", "AP respondeu a coleta", base, 1 if ok else 0)
    if not ok:
        return

    for line in radio.splitlines():
        m = re.search(
            r"\s(2\.4G|5G|6G)\s+\S+\s+\S+\s+(\d+)/(\d+)M\+?\s+(\d+)/(\d+)\s+(\d+)\s+(\d+)%\s+(\d+)/",
            line,
        )
        if not m:
            continue
        band, channel, bw, power, max_power, clients, util, inter = m.groups()
        labels = base + [("band", band)]
        metrics.add("huawei_radio_channel", "Canal do radio", labels, channel)
        metrics.add("huawei_radio_bandwidth_mhz", "Largura do canal em MHz", labels, bw)
        metrics.add("huawei_radio_power_dbm", "EIRP atual em dBm", labels, power)
        metrics.add("huawei_radio_max_power_dbm", "EIRP maximo do radio em dBm", labels, max_power)
        metrics.add("huawei_radio_clients", "Clientes associados no radio", labels, clients)
        metrics.add("huawei_radio_utilization_percent", "Ocupacao do canal", labels, util)
        metrics.add("huawei_radio_interference_percent", "Interferencia co-canal", labels, inter)

    for line in out.get("display station all", "").splitlines():
        parts = line.split()
        if len(parts) < 11 or not re.match(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$", parts[0]):
            continue
        mac, _apid, _name, _rf, band, phy, rates, rssi, _vlan, ip, ssid = parts[:11]
        rx, _, tx = rates.partition("/")
        labels = base + [
            ("band", band), ("ssid", ssid), ("mac", mac.replace("-", "")), ("phy", phy), ("sta_ip", ip),
        ]
        metrics.add("huawei_sta_rssi_dbm", "RSSI do cliente", labels, rssi)
        metrics.add("huawei_sta_rx_mbps", "Taxa negociada de recepcao do cliente", labels, rx)
        metrics.add("huawei_sta_tx_mbps", "Taxa negociada de transmissao do cliente", labels, tx)

    for line in out.get("display vap all", "").splitlines():
        m = re.match(r"^\s*\d+\s+\S+\s+(\d+)\s+\d+\s+([0-9A-Fa-f-]{14})\s+(\S+)\s+\S+\s+(\d+)\s+(\S+)", line)
        if m:
            rfid, _bssid, status, sta, ssid = m.groups()
            labels = base + [("radio", rfid), ("ssid", ssid)]
            metrics.add("huawei_vap_up", "1 se o SSID esta no ar nesse radio", labels, 1 if status == "ON" else 0)
            metrics.add("huawei_vap_clients", "Clientes no SSID nesse radio", labels, sta)

    version = out.get("display version", "")
    m = re.search(r"uptime is (\d+) weeks?, (\d+) days?, (\d+) hours?, (\d+) minutes?", version)
    if m:
        w, d, h, mi = (int(x) for x in m.groups())
        metrics.add("huawei_ap_uptime_seconds", "Tempo ligado do AP", base, ((w * 7 + d) * 24 + h) * 3600 + mi * 60)
    m = re.search(r"Version \S+ \(([^)]+)\)", version)
    if m:
        metrics.add("huawei_ap_info", "Versao do firmware", base + [("version", m.group(1))], 1)

    health = out.get("display health", "")
    for m in re.finditer(r"^\s*\d+\s+-\s+\d+\s+(\S+)\s+(\S+)\s+-?\d+\s+-?\d+\s+(-?\d+)\s*$", health, re.M):
        metrics.add("huawei_ap_temperature_celsius", "Temperatura do sensor", base + [("sensor", m.group(1))], m.group(3))
    m = re.search(r"^\s*0\s+(\d+)\s*%\s+\d+\s*%\s*$", health, re.M)
    if m:
        metrics.add("huawei_ap_cpu_percent", "Uso de CPU", base, m.group(1))
    m = re.search(r"^\s*0\s+\d+\s+\d+\s+(\d+)\s*%\s+\d+\s*%\s*$", health, re.M)
    if m:
        metrics.add("huawei_ap_memory_percent", "Uso de memoria", base, m.group(1))
    for m in re.finditer(r"^\s*0\s+(\w+):\s+\d+\s+\d+\s+(\d+)\s*%", health, re.M):
        metrics.add("huawei_ap_storage_percent", "Uso da flash", base + [("partition", m.group(1))], m.group(2))

    add_flags(
        metrics, base,
        0 if re.search(r"^\s*led off\s*$", out.get("display current-configuration | include led", ""), re.M) else 1,
        1 if "stelnet server enable" in out.get("display current-configuration | include stelnet", "") else 0,
    )

    iface = ap.get("uplink", "GigabitEthernet0/0/0")
    text = extra.get(f"display interface {iface}", "")
    if "current state" in text:
        def grab(pattern):
            m = re.search(pattern, text)
            return m.group(1) if m else None

        input_block = text.split("Output:", 1)[0]
        output_block = text.split("Output:", 1)[1] if "Output:" in text else ""
        rx_err = re.search(r"Total Error:\s*(\d+)", input_block)
        tx_err = re.search(r"Total Error:\s*(\d+)", output_block)
        add_uplink(
            metrics, base, iface,
            1 if re.search(r"current state : UP", text) else 0,
            grab(r"Speed : (\d+)"),
            grab(r"Input:\s+\d+ packets, (\d+) bytes"),
            grab(r"Output:\s+\d+ packets, (\d+) bytes"),
            rx_err.group(1) if rx_err else None,
            tx_err.group(1) if tx_err else None,
            grab(r"CRC:\s+(\d+)"),
        )


# ---------------------------------------------------------------- MD-CLI


def parse_mdcli(ap, out, metrics, extra):
    base = base_labels(ap)
    radios = json_after(out.get("display wifi-radio/wifi-radio-statistics/", "")).get("wifi-radio-statistic") or []
    metrics.add("huawei_ap_up", "AP respondeu a coleta", base, 1 if radios else 0)
    if not radios:
        return

    for r in radios:
        labels = base + [("band", BAND.get(r.get("frequency-band"), str(r.get("frequency-band"))))]
        metrics.add("huawei_radio_channel", "Canal do radio", labels, r.get("channel"))
        metrics.add("huawei_radio_bandwidth_mhz", "Largura do canal em MHz", labels, BW.get(r.get("bandwidth")))
        metrics.add("huawei_radio_power_dbm", "EIRP atual em dBm", labels, r.get("power"))
        metrics.add("huawei_radio_max_power_dbm", "EIRP maximo do radio em dBm", labels, r.get("max-txpower"))
        metrics.add("huawei_radio_clients", "Clientes associados no radio", labels, r.get("conneted-terminals"))
        metrics.add("huawei_radio_utilization_percent", "Ocupacao do canal", labels, r.get("channel-utilization"))
        metrics.add("huawei_radio_rx_utilization_percent", "Parte da ocupacao recebendo", labels, r.get("rx-utilization"))
        metrics.add("huawei_radio_tx_utilization_percent", "Parte da ocupacao transmitindo", labels, r.get("tx-utilization"))
        metrics.add("huawei_radio_interference_percent", "Interferencia no canal", labels, r.get("interference-ratio"))
        metrics.add("huawei_radio_adjacent_interference_percent", "Interferencia de canal adjacente", labels, r.get("adjacent-interference"))
        metrics.add("huawei_radio_noise_dbm", "Piso de ruido em dBm", labels, r.get("noise-intensity"))
        metrics.add("huawei_radio_retrans_percent", "Retransmissao no radio", labels, r.get("retrans-rate"))
        metrics.add("huawei_radio_packet_loss_percent", "Perda de pacote no radio", labels, r.get("packet-loss-rate"))
        metrics.add("huawei_radio_up_bps", "Trafego clientes para o AP em bits/s", labels, r.get("upward-speed"))
        metrics.add("huawei_radio_down_bps", "Trafego AP para clientes em bits/s", labels, r.get("downward-speed"))
        metrics.add("huawei_radio_crc_error_ppdu", "PPDUs com erro de CRC no ultimo intervalo", labels, r.get("crc-err-ppdu-num"))
        metrics.add("huawei_radio_spatial_streams", "Fluxos espaciais (NSS) do radio", labels, r.get("nss"))

    for s in json_after(out.get("display wifi-sta/wifi-sta-statistics/", "")).get("wifi-sta-statistic") or []:
        account = str(s.get("account") or "")
        labels = base + [
            ("band", BAND.get(s.get("frequency-band"), str(s.get("frequency-band")))),
            ("ssid", s.get("ssid") or ""),
            ("mac", account if re.fullmatch(r"[0-9a-fA-F]{12}", account) else ""),
            ("phy", PHY.get(s.get("mode"), str(s.get("mode")))),
            ("hostname", s.get("host-name") or ""),
            ("sta_ip", b64_ip(s.get("terminal-ip") or "")),
        ]
        metrics.add("huawei_sta_rssi_dbm", "RSSI do cliente", labels, s.get("rssi"))
        metrics.add("huawei_sta_snr_db", "SNR do cliente", labels, s.get("signal-noise-ratio"))
        metrics.add("huawei_sta_link_mbps", "Taxa do link do cliente", labels, s.get("uplink-speed"))
        metrics.add("huawei_sta_mcs", "MCS em uso", labels, s.get("mcs"))
        metrics.add("huawei_sta_spatial_streams", "Fluxos espaciais (NSS) do cliente", labels, s.get("maximum-nss"))
        metrics.add("huawei_sta_delay_ms", "Atraso medio do cliente", labels, s.get("delay"))
        metrics.add("huawei_sta_max_delay_ms", "Maior atraso do cliente no intervalo", labels, s.get("max-delay"))
        metrics.add("huawei_sta_retrans_percent", "Retransmissao do cliente", labels, s.get("retrans-rate"))
        metrics.add("huawei_sta_packet_loss_percent", "Perda de pacote do cliente", labels, s.get("packet-loss-rate"))
        metrics.add("huawei_sta_up_kbps", "Trafego do cliente para o AP em kbit/s", labels, s.get("upward-speed"))
        metrics.add("huawei_sta_down_kbps", "Trafego do AP para o cliente em kbit/s", labels, s.get("downward-speed"))
        metrics.add("huawei_sta_online_seconds", "Tempo conectado", labels, s.get("online-time"))

    text = out.get("display cpu-memory all", "")
    m = re.search(r'"system-cpu-usage":\s*(\d+)', text)
    if m:
        metrics.add("huawei_ap_cpu_percent", "Uso de CPU", base, m.group(1))
    m = re.search(r'"os-memory-usage":\s*(\d+)', text)
    if m:
        metrics.add("huawei_ap_memory_percent", "Uso de memoria", base, m.group(1))
    for p in (json_after(text).get("board-storage-partition-infos") or {}).get("board-storage-partition-info") or []:
        metrics.add(
            "huawei_ap_storage_percent", "Uso da flash",
            base + [("partition", p.get("partition-mounted-info") or p.get("partition-name"))],
            p.get("partition-memory-usage"),
        )

    add_flags(
        metrics, base,
        0 if '"led-switch": "turn-off"' in out.get("display wlan-system-profile/system-profiles/", "") else 1,
        1 if '"stelnet-ipv4-enable": "enable"' in out.get("display sshs/server-enable/", "") else 0,
    )

    iface = ap.get("uplink", "MultiGE0/0/0")
    stats = json_after(extra.get(f'display ifm/interfaces/interface[name="{iface}"]/mib-statistics/', ""))
    dyn = json_after(extra.get(f'display ifm/interfaces/interface[name="{iface}"]/dynamic/', ""))
    if stats or dyn:
        bw = num(dyn.get("bandwidth"))
        add_uplink(
            metrics, base, iface,
            1 if dyn.get("oper-status") == "up" else 0,
            bw / 1e6 if bw else None,
            stats.get("receive-byte"),
            stats.get("send-byte"),
            stats.get("receive-error-packet"),
            stats.get("send-error-packet"),
            (stats.get("huawei-pic:eth-port-err-sts") or {}).get("rx-crc"),
        )


def uplink_commands(ap):
    if ap["cli"] == "vrp":
        return [f"display interface {ap.get('uplink', 'GigabitEthernet0/0/0')}"]
    iface = ap.get("uplink", "MultiGE0/0/0")
    return [
        f'display ifm/interfaces/interface[name="{iface}"]/mib-statistics/',
        f'display ifm/interfaces/interface[name="{iface}"]/dynamic/',
    ]


def collect(config, password):
    metrics = Metrics()
    user = config.get("user", "admin")
    for ap in config["aps"]:
        base = base_labels(ap)
        started = time.time()
        try:
            main_cmds = VRP_COMMANDS if ap["cli"] == "vrp" else MDCLI_COMMANDS
            extra_cmds = uplink_commands(ap)
            out = run_ssh(ap["host"], user, password, main_cmds + extra_cmds)
            extra = {c: out.get(c, "") for c in extra_cmds}
            (parse_vrp if ap["cli"] == "vrp" else parse_mdcli)(ap, out, metrics, extra)
        except Exception as exc:
            metrics.add("huawei_ap_up", "AP respondeu a coleta", base, 0)
            sys.stderr.write(f"{ap['name']}: {exc}\n")
        metrics.add("huawei_ap_scrape_seconds", "Tempo gasto lendo o AP", base, round(time.time() - started, 2))
    metrics.add("huawei_ap_collect_timestamp_seconds", "Epoch da ultima coleta", [], int(time.time()))
    return metrics.text()


def main():
    config = json.loads(Path(CONFIG_FILE).read_text())
    password = Path(PASS_FILE).read_text().strip()
    body = collect(config, password)
    if "--stdout" in sys.argv:
        sys.stdout.write(body)
        return
    dest = Path(OUT_FILE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".prom.tmp")
    tmp.write_text(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)


if __name__ == "__main__":
    main()
