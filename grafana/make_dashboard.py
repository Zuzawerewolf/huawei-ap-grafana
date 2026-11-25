#!/usr/bin/env python3
"""Gera o JSON do dashboard do Grafana para as métricas do huawei_ap_exporter.

  python3 make_dashboard.py > dashboard.json             # versão para importar
  python3 make_dashboard.py --layout tv > tv.json        # versão compacta (cabe em 1366x768)

Opções:
  --layout full|tv      completa (padrão) ou compacta para TV
  --datasource UID      UID do Prometheus; sem isso o JSON pede o datasource na importação
  --uid / --title       identificação do dashboard
  --colors arquivo.json {"sala 5G": "#38BDF8", ...} cores fixas por "site banda"
  --header arquivo.md   texto livre no topo
  --mask                esconde nome, IP, MAC e SSID dos clientes (para print público)
"""
import argparse
import json
from pathlib import Path

GREEN, YELLOW, RED, BLUE = "#34D399", "#FBBF24", "#F87171", "#38BDF8"


class Builder:
    def __init__(self, ds, colors, mask):
        self.ds = ds
        self.colors = colors
        self.mask = mask
        self.panels = []
        self.next_id = 1

    # ------------------------------------------------------------ base

    def add(self, panel, x, y, w, h):
        panel["id"] = self.next_id
        self.next_id += 1
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        if panel["type"] not in ("row", "text"):
            panel["datasource"] = self.ds
            for t in panel.get("targets", []):
                t["datasource"] = self.ds
        self.panels.append(panel)

    def row(self, title, y):
        self.add({"type": "row", "title": title, "collapsed": False, "panels": []}, 0, y, 24, 1)

    def color_overrides(self):
        return [
            {"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
            for name, c in self.colors.items()
        ]

    @staticmethod
    def target(expr, legend="", ref="A", **kw):
        t = {"expr": expr, "legendFormat": legend, "refId": ref}
        t.update(kw)
        return t

    @staticmethod
    def steps(*pairs, base=GREEN):
        s = [{"color": base, "value": None}]
        s += [{"color": c, "value": v} for v, c in pairs]
        return {"mode": "absolute", "steps": s}

    # ------------------------------------------------------------ painéis

    def stat(self, title, expr, unit="none", legend="{{site}}", thresholds=None, mappings=None,
             color_mode="value", graph="none", decimals=0, text_mode="value_and_name"):
        defaults = {
            "unit": unit, "noValue": "sem dado",
            "color": {"mode": "thresholds"},
            "thresholds": thresholds or self.steps(base=BLUE),
            "mappings": mappings or [],
        }
        if decimals is not None:
            defaults["decimals"] = decimals
        return {
            "type": "stat", "title": title,
            "targets": [self.target(expr, legend, instant=True)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {
                "colorMode": color_mode, "graphMode": graph, "justifyMode": "center",
                "textMode": text_mode, "orientation": "auto", "wideLayout": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "showPercentChange": False,
            },
        }

    def on_off(self, title, expr, on="ONLINE", off="OFFLINE", on_good=True):
        good, bad = (GREEN, RED) if on_good else (YELLOW, GREEN)
        return self.stat(
            title, expr, color_mode="background",
            thresholds={"mode": "absolute", "steps": [{"color": bad, "value": None}, {"color": good, "value": 1}]},
            mappings=[{"type": "value", "options": {
                "0": {"text": off, "color": bad, "index": 0},
                "1": {"text": on, "color": good, "index": 1}}}],
        )

    def gauge(self, title, expr, unit="percent", warn=75, crit=90, legend="{{site}}"):
        return {
            "type": "bargauge", "title": title,
            "targets": [self.target(expr, legend, instant=True)],
            "fieldConfig": {"defaults": {
                "unit": unit, "min": 0, "max": 100, "decimals": 0,
                "color": {"mode": "thresholds"},
                "thresholds": self.steps((warn, YELLOW), (crit, RED)),
            }, "overrides": []},
            "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                        "valueMode": "color", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        }

    def ts(self, title, targets, unit, legend=("mean", "max"), placement="bottom", fill=15, soft=None,
           limits=None, style="line", stack=False, interp="smooth", decimals=None, hide_legend=False,
           colored=True, negative_y=None):
        custom = {
            "drawStyle": style, "lineInterpolation": interp, "lineWidth": 0 if style == "bars" else 2,
            "fillOpacity": 75 if style == "bars" else fill, "gradientMode": "none" if style == "bars" else "opacity",
            "showPoints": "never", "spanNulls": True, "axisBorderShow": False,
            "thresholdsStyle": {"mode": "dashed" if limits else "off"},
        }
        if stack:
            custom["stacking"] = {"group": "A", "mode": "normal"}
        if soft:
            custom["axisSoftMin"], custom["axisSoftMax"] = soft
        defaults = {
            "unit": unit, "color": {"mode": "palette-classic"}, "custom": custom,
            "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}]
                           + [{"color": c, "value": v} for v, c in (limits or [])]},
        }
        if decimals is not None:
            defaults["decimals"] = decimals
        overrides = self.color_overrides() if colored else []
        if negative_y:
            overrides.append({
                "matcher": {"id": "byRegexp", "options": negative_y},
                "properties": [{"id": "custom.transform", "value": "negative-Y"}],
            })
        legend_opts = {"showLegend": not hide_legend, "displayMode": "table" if legend else "list",
                       "placement": placement, "calcs": list(legend or [])}
        if legend and legend[0] == "mean":
            legend_opts.update(sortBy="Mean", sortDesc=True)
        return {
            "type": "timeseries", "title": title,
            "targets": [self.target(e, l, chr(65 + i)) for i, (e, l) in enumerate(targets)],
            "fieldConfig": {"defaults": defaults, "overrides": overrides},
            "options": {"legend": legend_opts, "tooltip": {"mode": "multi", "sort": "desc"}},
        }

    def pie(self, title, expr, legend):
        return {
            "type": "piechart", "title": title,
            "targets": [self.target(expr, legend, instant=True)],
            "fieldConfig": {"defaults": {"color": {"mode": "palette-classic"}, "decimals": 0}, "overrides": []},
            "options": {"pieType": "donut", "displayLabels": ["percent"],
                        "legend": {"displayMode": "table", "placement": "right", "showLegend": True, "values": ["value"]},
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "tooltip": {"mode": "single"}},
        }

    def table(self, title, columns, by, sort=None, extra_exclude=()):
        """columns: [(nome, expr, unit, cell)] — cada expr vira uma coluna, unidas pelos rótulos em `by`."""
        targets, rename, index, overrides = [], {}, {}, []
        labels = [l for l, _ in by]
        for i, (_, label_name) in enumerate(by):
            rename[labels[i]] = label_name
            index[labels[i]] = i
        for i, (name, expr, unit, cell) in enumerate(columns):
            ref = chr(65 + i)
            targets.append(self.target(f"max by ({', '.join(labels)}) ({expr})", ref=ref, format="table", instant=True))
            rename[f"Value #{ref}"] = name
            index[f"Value #{ref}"] = len(by) + i
            props = [{"id": "unit", "value": unit}]
            if cell:
                props += cell
            overrides.append({"matcher": {"id": "byName", "options": name}, "properties": props})
        exclude = {"Time": True, **{k: True for k in extra_exclude}}
        return {
            "type": "table", "title": title, "targets": targets,
            "transformations": [
                {"id": "merge", "options": {}},
                {"id": "organize", "options": {"excludeByName": exclude, "indexByName": index, "renameByName": rename}},
            ],
            "fieldConfig": {"defaults": {"custom": {"align": "center", "cellOptions": {"type": "auto"}, "minWidth": 50},
                                         "noValue": "–", "decimals": 0}, "overrides": overrides},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                        "sortBy": [{"displayName": sort, "desc": True}] if sort else []},
        }

    @staticmethod
    def colored(*pairs, base=GREEN):
        return [{"id": "thresholds", "value": Builder.steps(*pairs, base=base)},
                {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]

    def state_timeline(self, title, expr):
        return {
            "type": "state-timeline", "title": title,
            "targets": [self.target(expr, "{{site}} {{band}}")],
            "fieldConfig": {"defaults": {"color": {"mode": "continuous-BlPu"}, "decimals": 0,
                                         "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
            "options": {"showValue": "always", "mergeValues": True, "rowHeight": 0.85, "alignValue": "center",
                        "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        }

    def bars(self, title, expr, legend, unit):
        return {
            "type": "bargauge", "title": title,
            "targets": [self.target(expr, legend, instant=True)],
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "continuous-BlPu"}}, "overrides": []},
            "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                        "valueMode": "color", "namePlacement": "left", "sizing": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        }


# ---------------------------------------------------------------- consultas

UP = "huawei_ap_up"
SB = "{{site}} {{band}}"
AVG5 = "avg_over_time({}[5m])"


def radio_table(b, only=None, title="Rádios"):
    pct = lambda w, c: b.colored((w, YELLOW), (c, RED))
    columns = [
            ("Canal", "huawei_radio_channel", "none", None),
            ("MHz", "huawei_radio_bandwidth_mhz", "none", None),
            ("EIRP", "huawei_radio_power_dbm", "dBm", None),
            ("Clientes", "huawei_radio_clients", "none", None),
            ("Ocupação", "huawei_radio_utilization_percent", "percent", pct(30, 60)),
            ("RX", "huawei_radio_rx_utilization_percent", "percent", None),
            ("TX", "huawei_radio_tx_utilization_percent", "percent", None),
            ("Interf.", "huawei_radio_interference_percent", "percent", pct(30, 60)),
            ("Adjac.", "huawei_radio_adjacent_interference_percent", "percent", pct(20, 40)),
            ("Ruído", "huawei_radio_noise_dbm", "dBm", b.colored((-85, YELLOW), (-75, RED))),
            ("Retrans.", "huawei_radio_retrans_percent", "percent", pct(10, 25)),
            ("Perda", "huawei_radio_packet_loss_percent", "percent", pct(1, 5)),
            ("↓ AP→cliente", "huawei_radio_down_bps", "bps", None),
            ("↑ cliente→AP", "huawei_radio_up_bps", "bps", None),
    ]
    if only:
        columns = [c for c in columns if c[0] in only]
    return b.table(title, columns, by=[("site", "AP"), ("band", "Banda")])


def client_table(b):
    exclude = ("hostname", "sta_ip", "mac", "ssid") if b.mask else ()
    return b.table(
        "Clientes conectados", [
            ("RSSI", "huawei_sta_rssi_dbm", "dBm", b.colored((-72, YELLOW), (-62, GREEN), base=RED)),
            ("SNR", "huawei_sta_snr_db", "dB", b.colored((20, YELLOW), (30, GREEN), base=RED)),
            ("Link", "huawei_sta_link_mbps or huawei_sta_tx_mbps", "Mbits",
             [{"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}},
              {"id": "min", "value": 0}, {"id": "max", "value": 2400},
              {"id": "thresholds", "value": b.steps((100, YELLOW), (400, GREEN), base=RED)}]),
            ("MCS", "huawei_sta_mcs", "none", None),
            ("NSS", "huawei_sta_spatial_streams", "none", None),
            ("Atraso", "huawei_sta_delay_ms", "ms", b.colored((20, YELLOW), (80, RED))),
            ("Retrans.", "huawei_sta_retrans_percent", "percent", b.colored((10, YELLOW), (25, RED))),
            ("Perda", "huawei_sta_packet_loss_percent", "percent", b.colored((1, YELLOW), (5, RED))),
            ("↓", "huawei_sta_down_kbps", "Kbits", None),
            ("↑", "huawei_sta_up_kbps", "Kbits", None),
            ("Conectado há", "huawei_sta_online_seconds", "s", None),
        ],
        by=[("site", "AP"), ("band", "Banda"), ("ssid", "SSID"), ("hostname", "Nome"),
            ("sta_ip", "IP"), ("phy", "Wi-Fi"), ("mac", "MAC")],
        sort="Link", extra_exclude=exclude,
    )


def overview_stats(b, y, h, widths):
    stats = [
        b.on_off("AP", UP),
        b.stat("Clientes", "sum by (site) (huawei_radio_clients)"),
        b.stat("CPU", "huawei_ap_cpu_percent", "percent", thresholds=b.steps((75, YELLOW), (90, RED))),
        b.stat("Memória", "huawei_ap_memory_percent", "percent", thresholds=b.steps((80, YELLOW), (90, RED))),
        b.stat("Temperatura", "max by (site) (huawei_ap_temperature_celsius)", "celsius",
               thresholds=b.steps((70, YELLOW), (85, RED))),
        b.stat("Ligado há", "huawei_ap_uptime_seconds", "s", decimals=None),
        b.stat("Uplink agora", "sum by (site) (rate(huawei_ap_uplink_receive_bytes_total[5m]) "
               "+ rate(huawei_ap_uplink_transmit_bytes_total[5m])) * 8", "bps", decimals=1),
        b.stat("Sinal médio", "avg by (site) (huawei_sta_rssi_dbm)", "dBm",
               thresholds=b.steps((-72, YELLOW), (-62, GREEN), base=RED)),
    ]
    x = 0
    for panel, w in zip(stats, widths):
        b.add(panel, x, y, w, h)
        x += w


def build_full(b, header):
    y = 0
    if header:
        b.add({"type": "text", "title": "", "transparent": True,
               "options": {"mode": "markdown", "content": header}}, 0, y, 24, 3)
        y += 3
    b.row("Visão geral", y); y += 1
    overview_stats(b, y, 4, [3] * 8); y += 4
    b.add(b.on_off("LED", "huawei_ap_led_on", "ACESO", "APAGADO", on_good=False), 0, y, 4, 3)
    b.add(b.on_off("SSH", "huawei_ap_ssh_up", "ABERTO", "FECHADO"), 4, y, 4, 3)
    b.add(b.on_off("Porta de uplink", "huawei_ap_uplink_up", "COM LINK", "SEM LINK"), 8, y, 4, 3)
    b.add(b.stat("Velocidade da porta", "huawei_ap_uplink_speed_mbps", "Mbits"), 12, y, 4, 3)
    b.add(b.gauge("Flash", "max by (site) (huawei_ap_storage_percent)", warn=80, crit=95), 16, y, 4, 3)
    b.add(b.stat("Tempo da coleta", "huawei_ap_scrape_seconds", "s", decimals=1,
                 thresholds=b.steps((20, YELLOW), (28, RED))), 20, y, 4, 3)
    y += 3

    b.row("Rádios", y); y += 1
    b.add(radio_table(b), 0, y, 24, 7); y += 7
    b.add(b.ts("Ocupação do canal · 2,4 GHz", [(AVG5.format('huawei_radio_utilization_percent{band="2.4G"}'), SB)],
               "percent", soft=(0, 100), limits=[(30, YELLOW), (60, RED)], placement="right"), 0, y, 12, 8)
    b.add(b.ts("Ocupação do canal · 5 e 6 GHz", [(AVG5.format('huawei_radio_utilization_percent{band!="2.4G"}'), SB)],
               "percent", soft=(0, 40), limits=[(30, YELLOW)], placement="right"), 12, y, 12, 8)
    y += 8
    b.add(b.ts("Tráfego por rádio (acima: AP → clientes · abaixo: clientes → AP)", [
        (AVG5.format("huawei_radio_down_bps"), SB + " AP→cli"),
        (AVG5.format("huawei_radio_up_bps"), SB + " cli→AP"),
    ], "bps", legend=("mean", "max"), colored=False, negative_y=".*cli→AP$"), 0, y, 12, 8)
    b.add(b.ts("Ocupação: transmitindo × recebendo", [
        (AVG5.format("huawei_radio_tx_utilization_percent"), SB + " TX"),
        (AVG5.format("huawei_radio_rx_utilization_percent"), SB + " RX"),
    ], "percent", colored=False, negative_y=".* RX$"), 12, y, 12, 8)
    y += 8
    b.add(b.ts("Clientes por rádio", [("huawei_radio_clients", SB)], "none", legend=("lastNotNull", "max"),
               style="bars", stack=True, decimals=0), 0, y, 8, 7)
    b.add(b.ts("Piso de ruído", [(AVG5.format("huawei_radio_noise_dbm"), SB)], "dBm", fill=0,
               limits=[(-85, YELLOW)]), 8, y, 8, 7)
    b.add(b.ts("Interferência (co-canal e adjacente)", [
        (AVG5.format("huawei_radio_interference_percent"), SB + " co-canal"),
        (AVG5.format("huawei_radio_adjacent_interference_percent"), SB + " adjacente"),
    ], "percent", colored=False, soft=(0, 60), limits=[(30, YELLOW)]), 16, y, 8, 7)
    y += 7
    b.add(b.ts("Retransmissão no rádio", [(AVG5.format("huawei_radio_retrans_percent"), SB)], "percent",
               soft=(0, 15), limits=[(10, YELLOW), (25, RED)]), 0, y, 8, 7)
    b.add(b.ts("Perda de pacote no rádio", [(AVG5.format("huawei_radio_packet_loss_percent"), SB)], "percent",
               soft=(0, 5), limits=[(1, YELLOW), (5, RED)]), 8, y, 8, 7)
    b.add(b.ts("Erros de CRC no ar (PPDU)", [(AVG5.format("huawei_radio_crc_error_ppdu"), SB)], "short",
               style="bars", stack=True, legend=("mean", "max")), 16, y, 8, 7)
    y += 7
    b.add(b.state_timeline("Canal usado (troca automática aparece aqui)", "huawei_radio_channel"), 0, y, 12, 6)
    b.add(b.ts("EIRP (potência)", [("huawei_radio_power_dbm", SB)], "dBm", legend=("lastNotNull",),
               interp="stepAfter", fill=0, placement="right"), 12, y, 12, 6)
    y += 6

    b.row("Clientes", y); y += 1
    b.add(b.ts("Sinal médio dos clientes", [("avg by (site, band) (" + AVG5.format("huawei_sta_rssi_dbm") + ")", SB)],
               "dBm", legend=("mean", "min"), soft=(-85, -40), limits=[(-72, RED), (-62, GREEN)], fill=8), 0, y, 8, 7)
    b.add(b.ts("Atraso dos clientes (média e pior)", [
        ("avg by (site, band) (" + AVG5.format("huawei_sta_delay_ms") + ")", SB + " média"),
        ("max by (site, band) (" + AVG5.format("huawei_sta_max_delay_ms") + ")", SB + " pior"),
    ], "ms", colored=False, limits=[(20, YELLOW), (80, RED)], fill=8), 8, y, 8, 7)
    b.add(b.ts("Taxa de link média", [
        ("avg by (site, band) (huawei_sta_link_mbps or huawei_sta_tx_mbps)", SB)], "Mbits",
        legend=("mean", "max"), interp="stepAfter", fill=10), 16, y, 8, 7)
    y += 7
    who = "{{site}} {{band}} {{phy}}" if b.mask else "{{hostname}} {{sta_ip}}"
    b.add(b.pie("Clientes por padrão Wi-Fi", "count by (phy) (huawei_sta_rssi_dbm)", "{{phy}}"), 0, y, 6, 8)
    if b.mask:
        b.add(b.pie("Clientes por banda", "count by (band) (huawei_sta_rssi_dbm)", "{{band}}"), 6, y, 6, 8)
    else:
        b.add(b.pie("Clientes por SSID e banda", "count by (ssid, band) (huawei_sta_rssi_dbm)", "{{ssid}} {{band}}"), 6, y, 6, 8)
    b.add(b.pie("Qualidade do sinal", 'label_replace(count(huawei_sta_rssi_dbm >= -62), "q", "bom", "", "") '
                'or label_replace(count(huawei_sta_rssi_dbm < -62 and huawei_sta_rssi_dbm >= -72), "q", "médio", "", "") '
                'or label_replace(count(huawei_sta_rssi_dbm < -72), "q", "fraco", "", "")', "{{q}}"), 12, y, 6, 8)
    b.add(b.bars("Quem mais baixa agora", "topk(8, huawei_sta_down_kbps)", who, "Kbits"), 18, y, 6, 8)
    y += 8
    b.add(client_table(b), 0, y, 24, 12); y += 12

    b.row("Uplink e hardware", y); y += 1
    b.add(b.ts("Tráfego no cabo do AP (acima: recebido · abaixo: enviado)", [
        ("rate(huawei_ap_uplink_receive_bytes_total[5m]) * 8", "{{site}} recebido"),
        ("rate(huawei_ap_uplink_transmit_bytes_total[5m]) * 8", "{{site}} enviado"),
    ], "bps", colored=False, negative_y=".*enviado$"), 0, y, 12, 7)
    b.add(b.ts("Erros na porta de uplink", [
        ("increase(huawei_ap_uplink_receive_errors_total[5m])", "{{site}} recepção"),
        ("increase(huawei_ap_uplink_transmit_errors_total[5m])", "{{site}} envio"),
        ("increase(huawei_ap_uplink_crc_errors_total[5m])", "{{site}} CRC"),
    ], "short", style="bars", colored=False, legend=("max",)), 12, y, 6, 7)
    b.add(b.ts("Temperatura", [("huawei_ap_temperature_celsius", "{{site}} {{sensor}}")], "celsius",
               colored=False, fill=0, legend=("lastNotNull", "max"), limits=[(70, YELLOW), (85, RED)]), 18, y, 6, 7)
    y += 7
    b.add(b.ts("CPU e memória", [
        ("huawei_ap_cpu_percent", "{{site}} CPU"), ("huawei_ap_memory_percent", "{{site}} memória")],
        "percent", colored=False, soft=(0, 100), fill=0, legend=("mean", "max")), 0, y, 12, 6)
    b.add(b.ts("Tempo da coleta por AP", [("huawei_ap_scrape_seconds", "{{site}}")], "s", colored=False,
               fill=0, legend=("mean", "max"), limits=[(25, RED)]), 12, y, 12, 6)


def build_tv(b):
    """Cabe numa tela 1366x768 em modo kiosk: 19 unidades de altura."""
    overview_stats(b, 0, 3, [3] * 8)
    b.add(radio_table(b, {"Canal", "MHz", "Clientes", "Ocupação", "Interf.", "Ruído", "Retrans."}, ""), 0, 3, 12, 6)
    b.add(b.ts("Ocupação do canal", [(AVG5.format("huawei_radio_utilization_percent"), SB)], "percent",
               legend=(), placement="right", soft=(0, 100), limits=[(30, YELLOW), (60, RED)]), 12, 3, 12, 6)
    b.add(b.ts("Tráfego por rádio", [
        (AVG5.format("huawei_radio_down_bps") + " + " + AVG5.format("huawei_radio_up_bps"), SB)], "bps",
        legend=(), hide_legend=True), 0, 9, 8, 5)
    b.add(b.ts("Clientes por rádio", [("huawei_radio_clients", SB)], "none", legend=(), hide_legend=True,
               style="bars", stack=True, decimals=0), 8, 9, 8, 5)
    b.add(b.ts("Sinal médio dos clientes", [("avg by (site, band) (" + AVG5.format("huawei_sta_rssi_dbm") + ")", SB)],
               "dBm", legend=(), hide_legend=True, soft=(-85, -40), limits=[(-72, RED), (-62, GREEN)], fill=8), 16, 9, 8, 5)
    b.add(b.ts("Atraso médio", [("avg by (site, band) (" + AVG5.format("huawei_sta_delay_ms") + ")", SB)], "ms",
               legend=(), hide_legend=True, limits=[(20, YELLOW), (80, RED)], fill=8), 0, 14, 8, 5)
    b.add(b.ts("Retransmissão", [(AVG5.format("huawei_radio_retrans_percent"), SB)], "percent",
               legend=(), hide_legend=True, soft=(0, 15), limits=[(10, YELLOW)]), 8, 14, 8, 5)
    b.add(b.ts("Tráfego no cabo", [
        ("sum by (site) (rate(huawei_ap_uplink_receive_bytes_total[5m]) + rate(huawei_ap_uplink_transmit_bytes_total[5m])) * 8",
         "{{site}}")], "bps", legend=(), placement="right"), 16, 14, 8, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layout", choices=["full", "tv"], default="full")
    ap.add_argument("--datasource")
    ap.add_argument("--uid", default="huawei-aps")
    ap.add_argument("--title", default="Wi-Fi · APs Huawei")
    ap.add_argument("--colors")
    ap.add_argument("--header")
    ap.add_argument("--mask", action="store_true")
    ap.add_argument("--tags", default="wifi,huawei")
    a = ap.parse_args()

    ds = {"type": "prometheus", "uid": a.datasource or "${DS_PROMETHEUS}"}
    colors = json.loads(Path(a.colors).read_text()) if a.colors else {}
    header = Path(a.header).read_text() if a.header else ""
    b = Builder(ds, colors, a.mask)
    if a.layout == "tv":
        build_tv(b)
    else:
        build_full(b, header)

    dash = {
        "uid": a.uid, "title": a.title, "tags": a.tags.split(","),
        "timezone": "browser", "refresh": "30s", "schemaVersion": 39, "graphTooltip": 1,
        "time": {"from": "now-3h" if a.layout == "tv" else "now-6h", "to": "now"},
        "panels": b.panels,
    }
    if a.layout == "tv":
        dash["timepicker"] = {"hidden": True}
    if not a.datasource:
        dash["__inputs"] = [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource",
                             "pluginId": "prometheus", "pluginName": "Prometheus"}]
    print(json.dumps(dash, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
