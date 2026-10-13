# VLESS+Reality Clash/Mihomo template.
# 实际节点由 scripts/11-output-nodes.py 生成到 /root/surelay-clash.yaml。

mixed-port: 7890
allow-lan: false
mode: rule
log-level: info
ipv6: false

proxies:
  - name: my-vps
    type: vless
    server: YOUR_SERVER_IP
    port: 443
    uuid: YOUR_VLESS_UUID
    flow: xtls-rprx-vision
    tls: true
    servername: www.microsoft.com
    client-fingerprint: chrome
    udp: true
    reality-opts:
      public-key: YOUR_REALITY_PUBLIC_KEY
      short-id: YOUR_REALITY_SHORT_ID
    alpn:
      - h2
      - http/1.1

proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - my-vps
      - DIRECT

rules:
  - MATCH,PROXY
