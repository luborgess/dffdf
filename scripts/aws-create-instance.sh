#!/bin/bash
# =============================================================================
# TelePi - AWS EC2 Instance Setup Script
# Cria instância EC2 c6in.xlarge otimizada para streaming
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# CONFIGURAÇÕES - AJUSTE CONFORME NECESSÁRIO
# -----------------------------------------------------------------------------

# Nome do projeto (usado para tags e security group)
PROJECT_NAME="telepi"

# Tipo de instância (c6in.xlarge = 4vCPU, 8GB RAM, 30Gbps rede)
# Alternativa econômica: c6in.large (~$84/mês)
INSTANCE_TYPE="c6in.xlarge"

# AMI Ubuntu 24.04 LTS (atualizar conforme região)
# us-east-1: ami-0866a3c8686eaeeba
# sa-east-1: ami-0fb4cf3a99aa89f72
AMI_ID="ami-0866a3c8686eaeeba"

# Região AWS
REGION="us-east-1"

# Key pair (deve existir previamente)
KEY_NAME="telepi-key"

# Tamanho do EBS em GB
EBS_SIZE=50

# -----------------------------------------------------------------------------
# CRIAR KEY PAIR (se não existir)
# -----------------------------------------------------------------------------

echo "🔑 Verificando key pair..."
if ! aws ec2 describe-key-pairs --key-names "$KEY_NAME" --region "$REGION" 2>/dev/null; then
    echo "  Criando key pair: $KEY_NAME"
    aws ec2 create-key-pair \
        --key-name "$KEY_NAME" \
        --region "$REGION" \
        --query 'KeyMaterial' \
        --output text > "${KEY_NAME}.pem"
    chmod 400 "${KEY_NAME}.pem"
    echo "  ✓ Key salva em ${KEY_NAME}.pem"
else
    echo "  ✓ Key pair já existe"
fi

# -----------------------------------------------------------------------------
# CRIAR SECURITY GROUP
# -----------------------------------------------------------------------------

echo "🔒 Configurando Security Group..."

# Verificar VPC padrão
VPC_ID=$(aws ec2 describe-vpcs \
    --region "$REGION" \
    --filters "Name=isDefault,Values=true" \
    --query 'Vpcs[0].VpcId' \
    --output text)

echo "  VPC: $VPC_ID"

# Criar security group
SG_NAME="${PROJECT_NAME}-sg"
SG_ID=$(aws ec2 describe-security-groups \
    --region "$REGION" \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' \
    --output text 2>/dev/null || echo "None")

if [ "$SG_ID" == "None" ] || [ -z "$SG_ID" ]; then
    echo "  Criando security group: $SG_NAME"
    SG_ID=$(aws ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "TelePi Streaming Cloner" \
        --vpc-id "$VPC_ID" \
        --region "$REGION" \
        --query 'GroupId' \
        --output text)
    
    # Regra SSH
    aws ec2 authorize-security-group-ingress \
        --group-id "$SG_ID" \
        --protocol tcp \
        --port 22 \
        --cidr 0.0.0.0/0 \
        --region "$REGION"
    
    echo "  ✓ Security group criado: $SG_ID"
else
    echo "  ✓ Security group já existe: $SG_ID"
fi

# -----------------------------------------------------------------------------
# CRIAR INSTÂNCIA EC2
# -----------------------------------------------------------------------------

echo "🖥️  Criando instância EC2..."

INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI_ID" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SG_ID" \
    --region "$REGION" \
    --block-device-mappings "[{
        \"DeviceName\": \"/dev/sda1\",
        \"Ebs\": {
            \"VolumeSize\": $EBS_SIZE,
            \"VolumeType\": \"gp3\",
            \"Iops\": 3000,
            \"Throughput\": 125,
            \"DeleteOnTermination\": true
        }
    }]" \
    --tag-specifications "[{
        \"ResourceType\": \"instance\",
        \"Tags\": [
            {\"Key\": \"Name\", \"Value\": \"$PROJECT_NAME\"},
            {\"Key\": \"Project\", \"Value\": \"$PROJECT_NAME\"}
        ]
    }]" \
    --query 'Instances[0].InstanceId' \
    --output text)

echo "  Instance ID: $INSTANCE_ID"

# Aguardar instância ficar running
echo "  Aguardando instância iniciar..."
aws ec2 wait instance-running \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION"

# -----------------------------------------------------------------------------
# OBTER IP PÚBLICO
# -----------------------------------------------------------------------------

PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --region "$REGION" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text)

# -----------------------------------------------------------------------------
# OUTPUT
# -----------------------------------------------------------------------------

echo ""
echo "============================================================"
echo "✅ INSTÂNCIA CRIADA COM SUCESSO!"
echo "============================================================"
echo ""
echo "Instance ID:  $INSTANCE_ID"
echo "Public IP:    $PUBLIC_IP"
echo "Instance:     $INSTANCE_TYPE"
echo "Region:       $REGION"
echo ""
echo "📡 Conectar via SSH:"
echo "   ssh -i ${KEY_NAME}.pem ubuntu@${PUBLIC_IP}"
echo ""
echo "📦 Próximos passos:"
echo "   1. scp -i ${KEY_NAME}.pem -r . ubuntu@${PUBLIC_IP}:~/telepi"
echo "   2. ssh -i ${KEY_NAME}.pem ubuntu@${PUBLIC_IP}"
echo "   3. cd telepi && chmod +x scripts/*.sh"
echo "   4. sudo ./scripts/network-tuning.sh"
echo "   5. ./scripts/setup.sh"
echo ""
echo "💰 Custo estimado: ~\$140/mês (c6in.xlarge)"
echo "============================================================"
