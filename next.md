✅ INSTANCE CREATED SUCCESSFULLY!
============================================================
Instance ID:  i-0eb9f37f8fc6886bd
Public IP:    35.170.74.93
Instance:     c6in.xlarge
Region:       us-east-1

📡 Connect via SSH:
   ssh -i telepi-key.pem ubuntu@35.170.74.93

📦 Next steps:
   1. pip install -r requirements.txt (if not done)
   2. scp -i telepi-key.pem -r . ubuntu@35.170.74.93:~/telepi
   3. ssh -i telepi-key.pem ubuntu@35.170.74.93
   4. cd telepi && chmod +x scripts/*.sh
   5. sudo ./scripts/network-tuning.sh
   6. ./scripts/setup.sh

💰 Estimated cost: ~$140/month (c6in.xlarge)