# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TelePi** is a high-performance Telegram group cloner that implements real-time streaming for large files (up to 2GB+). The project minimizes memory and disk usage through a streaming architecture where download and upload happen in parallel.

## Technology Stack

- **Language**: Python 3.12+
- **Core Library**: Telethon (Telegram MTProto client)
- **Infrastructure**: AWS EC2 (c6in.xlarge recommended for high throughput)
- **Optional**: cryptg (crypto acceleration), boto3 (AWS SDK)

## Key Architecture

### Streaming Pipeline

The core innovation is parallel download/upload streaming:

```
Telegram (source) → EC2 Buffer (~500MB) → Telegram (destination)
     ↓                      ↓                      ↓
 chunk 1 (512KB)    → saveBigFilePart  → upload chunk 1
 chunk 2 (512KB)    → (discard chunk 1) → upload chunk 2
     ...                  ...                    ...
 chunk N            → chunk N           → upload chunk N
```

**Never holds complete file in memory** - maintains ~500MB buffer instead of 2GB+.

### Key Classes

- `StreamingUploader` (clone_streaming.py:138-206): Handles chunked parallel uploads using `SaveBigFilePartRequest`
- `StreamingCloner` (clone_streaming.py:213-401): Main orchestrator with rate limiting and message cloning
- Forum topic support with auto-creation and mapping via `topic_map.json`

### Checkpoint System

Progress is saved to `checkpoint.txt` after each message, enabling resumption from interruptions. This is critical for long-running operations with rate limits.

## Development Commands

```bash
# Pre-flight check
python check_setup.py

# Get chat IDs for configuration
python get_chat_ids.py

# Main cloner (streaming mode)
python clone_streaming.py

# Run in background
nohup python clone_streaming.py > output.log 2>&1 &

# Monitor progress
tail -f output.log
```

### Environment Setup

```bash
# Virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment (create from template if needed)
cp .env.example .env
nano .env
export $(grep -v '^#' .env | xargs)
```

### AWS EC2 Deployment (scripts/)

```bash
# Create instance
chmod +x scripts/aws-create-instance.sh
./scripts/aws-create-instance.sh

# On EC2: network tuning (kernel optimization)
sudo ./scripts/network-tuning.sh

# On EC2: environment setup
./scripts/setup.sh
```

## Configuration

### Required Environment Variables (.env)

```bash
TG_API_ID="your_api_id"              # From my.telegram.org
TG_API_HASH="your_api_hash"          # From my.telegram.org
SOURCE_CHAT="-100123456789"          # Source group/channel ID
TARGET_CHAT="-100987654321"          # Destination group/channel ID
SOURCE_TOPIC="123"                   # Optional: Source topic ID
TARGET_TOPIC="456"                   # Optional: Destination topic ID
AUTO_CREATE_TOPICS="true"            # Auto-create topics in destination
```

### Constants in clone_streaming.py

- `CHUNK_SIZE = 512 * 1024` (512KB - MTProto max)
- `PARALLEL_UPLOADS = 10` (concurrent chunk uploads)
- `MIN_INTERVAL = 1.3` (seconds between messages ~46 msg/min)

## Rate Limiting

Telegram enforces ~20-50 messages/minute. The script:
- Uses `MIN_INTERVAL = 1.3` seconds (~46 msg/min - safe within limits)
- Handles `FloodWaitError` with exponential backoff
- Respects rate limits for both message sending and file operations

## Performance Characteristics

- **Memory**: ~500MB buffer (constant) vs 2GB+ for traditional methods
- **Speed**: ~4 minutes for 2GB video (vs ~7 min traditional)
- **Throughput**: Limited by EC2 network (~30Gbps max)
- **Bottleneck**: Telegram rate limits, not network/compute

## File Structure

```
├── clone_streaming.py    # Main implementation (489 lines)
├── get_chat_ids.py       # Chat discovery utility
├── check_setup.py        # Configuration validator
├── requirements.txt      # Python dependencies
├── scripts/
│   ├── aws-create-instance.sh  # EC2 provisioning
│   ├── network-tuning.sh       # Kernel optimization
│   └── setup.sh                # Environment setup
├── checkpoint.txt        # Resume state (auto-generated)
├── topic_map.json        # Topic ID mapping (auto-generated)
└── clone.log             # Runtime log
```

## Important Implementation Details

1. **Message iteration**: Uses `client.iter_messages()` with `reverse=True` for chronological processing
2. **Topic filtering**: Messages filtered by `reply_to_msg_id` or `reply_to.reply_to_top_id`
3. **File size handling**: Files <10MB downloaded to memory; >=10MB use streaming
4. **Small files**: Use `client.download_media(file=bytes)` for in-memory download
5. **Large files**: Use `client.iter_download()` with `StreamingUploader` for parallel chunked upload

## Working with Checkpoints

- `checkpoint.txt` stores the last successfully processed message ID
- Resume is automatic: `min_id=last_id` in `iter_messages()`
- Delete `checkpoint.txt` to restart from beginning

## Forum Topics

When `AUTO_CREATE_TOPICS=true`:
- Source topics are automatically created in destination
- Topic ID mapping persisted to `topic_map.json`
- Messages only cloned if they match the source topic filter

## Security Considerations

- Never commit `.env` files (contains API credentials)
- AWS credentials must be secured
- SSH key pairs for EC2 access
- Telegram API keys from my.telegram.org must be kept private
