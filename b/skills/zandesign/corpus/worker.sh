#!/bin/bash
cd "$(dirname "$0")"
mkdir -p videos transcripts
while read -r id; do
  [ -z "$id" ] && continue
  if [ ! -s "videos/${id}.mp4" ] && [ ! -f "videos/${id}.err" ]; then
    if ! yt-dlp -q --no-warnings -f "mp4/best" --playlist-items 1 \
         -o "videos/${id}.%(ext)s" \
         "https://x.com/zander_supafast/status/${id}" 2>> download.log; then
      echo "$id" >> failed.txt
      touch "videos/${id}.err"
    fi
    sleep 1
  fi
  if [ -s "videos/${id}.mp4" ] && [ ! -s "transcripts/${id}.txt" ] && [ ! -f "transcripts/${id}.err" ]; then
    if ! whisper "videos/${id}.mp4" --model base.en --language en \
         --output_format txt --output_dir transcripts --fp16 False --verbose False >> whisper.log 2>&1; then
      touch "transcripts/${id}.err"
    fi
  fi
done < "$1"
echo "worker $1 done"
