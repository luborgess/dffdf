#!/usr/bin/env python3
# =============================================================================
# TelePi - AWS EC2 Instance Setup Script (Python Version)
# Creates a c6in.xlarge EC2 instance optimized for streaming
# =============================================================================

import boto3
import os
import sys
import time
from botocore.exceptions import ClientError

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

PROJECT_NAME = "telepi"
# Instance Type (c6in.xlarge = 4vCPU, 8GB RAM, 30Gbps net)
INSTANCE_TYPE = "c6in.xlarge"
# AMI Ubuntu 24.04 LTS (us-east-1)
AMI_ID = "ami-0866a3c8686eaeeba"
REGION = "us-east-1"
KEY_NAME = "telepi-key"
EBS_SIZE = 50

def main():
    print(f"🚀 Starting TelePi AWS Setup ({REGION})")
    
    # Initialize session
    try:
        ec2 = boto3.client('ec2', region_name=REGION)
        ec2_resource = boto3.resource('ec2', region_name=REGION)
    except Exception as e:
        print(f"❌ Error initializing AWS client: {e}")
        print("   Make sure you have configured your AWS credentials (aws configure).")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 1. KEY PAIR
    # -------------------------------------------------------------------------
    print("🔑 Checking key pair...")
    key_file = f"{KEY_NAME}.pem"
    
    try:
        ec2.describe_key_pairs(KeyNames=[KEY_NAME])
        print(f"   ✓ Key pair '{KEY_NAME}' already exists")
    except ClientError as e:
        if 'InvalidKeyPair.NotFound' in str(e):
            print(f"   Creating key pair: {KEY_NAME}")
            try:
                key_pair = ec2.create_key_pair(KeyName=KEY_NAME)
                with open(key_file, "w") as f:
                    f.write(key_pair['KeyMaterial'])
                
                # Best effort permission set for Windows/Linux
                try:
                    os.chmod(key_file, 0o400)
                except Exception:
                    pass
                    
                print(f"   ✓ Key saved to {key_file}")
            except Exception as kpe:
                print(f"❌ Error creating key pair: {kpe}")
                sys.exit(1)
        else:
            print(f"❌ Error checking key pair: {e}")
            sys.exit(1)

    # -------------------------------------------------------------------------
    # 2. SECURITY GROUP
    # -------------------------------------------------------------------------
    print("🔒 Configuring Security Group...")
    sg_name = f"{PROJECT_NAME}-sg"
    sg_id = None
    vpc_id = None

    # Get Default VPC
    try:
        vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])
        if not vpcs['Vpcs']:
            print("❌ No default VPC found.")
            sys.exit(1)
        vpc_id = vpcs['Vpcs'][0]['VpcId']
        print(f"   VPC: {vpc_id}")
    except Exception as e:
        print(f"❌ Error getting VPC: {e}")
        sys.exit(1)

    # Check/Create Security Group
    try:
        sgs = ec2.describe_security_groups(
            Filters=[{'Name': 'group-name', 'Values': [sg_name]}]
        )
        if sgs['SecurityGroups']:
            sg_id = sgs['SecurityGroups'][0]['GroupId']
            print(f"   ✓ Security group '{sg_name}' already exists: {sg_id}")
        else:
            print(f"   Creating security group: {sg_name}")
            sg = ec2.create_security_group(
                GroupName=sg_name,
                Description="TelePi Streaming Cloner",
                VpcId=vpc_id
            )
            sg_id = sg['GroupId']
            
            # Authorize Ingress (SSH)
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': 22,
                    'ToPort': 22,
                    'IpRanges': [{'CidrIp': '0.0.0.0/0'}]
                }]
            )
            print(f"   ✓ Security group created: {sg_id}")
            
    except Exception as e:
        print(f"❌ Error managing security group: {e}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # 3. CREATE INSTANCE
    # -------------------------------------------------------------------------
    print("🖥️  Creating EC2 instance...")
    
    try:
        instances = ec2_resource.create_instances(
            ImageId=AMI_ID,
            InstanceType=INSTANCE_TYPE,
            KeyName=KEY_NAME,
            SecurityGroupIds=[sg_id],
            MaxCount=1,
            MinCount=1,
            BlockDeviceMappings=[{
                'DeviceName': '/dev/sda1',
                'Ebs': {
                    'VolumeSize': EBS_SIZE,
                    'VolumeType': 'gp3',
                    'Iops': 3000,
                    'Throughput': 125,
                    'DeleteOnTermination': True
                }
            }],
            TagSpecifications=[{
                'ResourceType': 'instance',
                'Tags': [
                    {'Key': 'Name', 'Value': PROJECT_NAME},
                    {'Key': 'Project', 'Value': PROJECT_NAME}
                ]
            }]
        )
        
        instance = instances[0]
        print(f"   Instance ID: {instance.id}")
        
        print("   Waiting for instance to be running...")
        instance.wait_until_running()
        
        # Reload to get public IP
        instance.reload()
        public_ip = instance.public_ip_address
        
        print("\n" + "="*60)
        print("✅ INSTANCE CREATED SUCCESSFULLY!")
        print("="*60)
        print(f"Instance ID:  {instance.id}")
        print(f"Public IP:    {public_ip}")
        print(f"Instance:     {INSTANCE_TYPE}")
        print(f"Region:       {REGION}")
        print("")
        print("📡 Connect via SSH:")
        print(f"   ssh -i {key_file} ubuntu@{public_ip}")
        print("")
        print("📦 Next steps:")
        print(f"   1. pip install -r requirements.txt (if not done)")
        print(f"   2. scp -i {key_file} -r . ubuntu@{public_ip}:~/telepi")
        print(f"   3. ssh -i {key_file} ubuntu@{public_ip}")
        print(f"   4. cd telepi && chmod +x scripts/*.sh")
        print(f"   5. sudo ./scripts/network-tuning.sh")
        print(f"   6. ./scripts/setup.sh")
        print("")
        print("💰 Estimated cost: ~$140/month (c6in.xlarge)")
        print("="*60)

    except Exception as e:
        print(f"❌ Error creating instance: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
