#!/bin/bash
# =============================================================================
# TelePi - Network Tuning para Alta Performance
# Otimizações de kernel para throughput de 30Gbps
# EXECUTAR COMO ROOT: sudo ./network-tuning.sh
# =============================================================================

set -e

echo "🔧 Aplicando tuning de rede para alta performance..."

# Criar arquivo de configuração sysctl
cat << 'EOF' | sudo tee /etc/sysctl.d/99-telepi-network.conf
# =============================================================================
# TelePi Network Tuning
# =============================================================================

# Buffer sizes para alta velocidade
net.core.rmem_max = 268435456
net.core.wmem_max = 268435456
net.core.rmem_default = 16777216
net.core.wmem_default = 16777216

# TCP buffer sizes
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728

# BBR congestion control (melhor para alta latência)
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq

# Backlog e conexões
net.core.netdev_max_backlog = 250000
net.core.somaxconn = 65535

# TCP Keepalive
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 60
net.ipv4.tcp_keepalive_probes = 3

# Reuso de conexões
net.ipv4.tcp_tw_reuse = 1

# Desabilitar slow start após idle
net.ipv4.tcp_slow_start_after_idle = 0

# File descriptors
fs.file-max = 2097152
fs.nr_open = 2097152
EOF

# Aplicar configurações
sudo sysctl -p /etc/sysctl.d/99-telepi-network.conf

# Aumentar limits de file descriptors
cat << 'EOF' | sudo tee /etc/security/limits.d/99-telepi.conf
* soft nofile 1048576
* hard nofile 1048576
* soft nproc 65535
* hard nproc 65535
root soft nofile 1048576
root hard nofile 1048576
EOF

# Adicionar ao systemd (para serviços)
sudo mkdir -p /etc/systemd/system.conf.d
cat << 'EOF' | sudo tee /etc/systemd/system.conf.d/99-telepi.conf
[Manager]
DefaultLimitNOFILE=1048576
DefaultLimitNPROC=65535
EOF

echo ""
echo "✅ Tuning de rede aplicado!"
echo ""
echo "📋 Configurações aplicadas:"
echo "   • TCP Buffer: até 128MB"
echo "   • Congestion Control: BBR"
echo "   • File Descriptors: 1M+"
echo "   • Queue Discipline: FQ"
echo ""
echo "⚠️  IMPORTANTE: Reinicie a sessão SSH ou reboot para aplicar limits"
echo ""
