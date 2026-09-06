"""Declarative feed manifest per docs/scraping.md. Order defines scrape precedence."""

from __future__ import annotations

from engine.models import SourceManifest

SOURCES: tuple[SourceManifest, ...] = (
    SourceManifest(
        name="ebrasha",
        urls=(
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/V2Ray-Config-By-EbraSha-All-Type.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/vless_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/vmess_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/ss_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/ssr_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/trojan_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/tuic_configs.txt",
            "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/main/separated-protocols/hysteria2_configs.txt",
        ),
        cadence_s=900,
        encoding="plain",
        line_separated=True,
        license="free-to-use per upstream repo",
        protocols=("vless", "vmess", "ss", "ssr", "trojan", "tuic", "hysteria2"),
    ),
    SourceManifest(
        name="epodonios",
        urls=(
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/vless.txt",
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/vmess.txt",
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/ss.txt",
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/ssr.txt",
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt",
            # All_Configs_Sub.txt is plain per-line URI lists; the repo's
            # whole-file base64 combined feed is All_Configs_base64_Sub.txt.
            "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/All_Configs_Sub.txt",
        ),
        cadence_s=300,
        encoding="plain",
        line_separated=True,
        license="GPL-3.0",
        protocols=("vless", "vmess", "ss", "ssr", "trojan", "tuic", "hysteria2"),
    ),
    SourceManifest(
        name="v2rayfree",
        urls=("https://raw.githubusercontent.com/free-nodes/v2rayfree/main/sub",),
        cadence_s=21600,
        encoding="base64",
        line_separated=False,
        license="per upstream repo",
        protocols=("ss", "vless", "vmess", "ssr", "trojan", "tuic", "hysteria2"),
    ),
    SourceManifest(
        name="gfpcom",
        urls=(
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/http2.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks4.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/socks5.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/ss.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/ssr.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/trojan.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/tuic.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/vless.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/vmess.txt",
            "https://raw.githubusercontent.com/wiki/gfpcom/free-proxy-list/lists/wireguard.txt",
        ),
        cadence_s=1800,
        encoding="plain",
        line_separated=True,
        license="per upstream repo",
        protocols=("http", "socks5", "ss", "ssr", "trojan", "tuic", "vless", "vmess"),
    ),
    SourceManifest(
        name="freefolkson",
        urls=(
            "https://raw.githubusercontent.com/FreeFolksOn/abc-configs-free-vpn-proxy-list/main/README.md",
        ),
        cadence_s=600,
        encoding="plain",
        line_separated=True,
        license="per upstream repo",
        protocols=("vless", "vmess", "ss", "ssr", "trojan", "tuic", "hysteria2"),
    ),
)
