MAX_SCAN_CHANNELS = 32
CONCURRENCY = 8
FFPROBE_BASE_ARGS = [
    "ffprobe",
    "-v", "error",
    "-rtsp_transport", "tcp",
    "-analyzeduration", "0",
    "-probesize", "32",
    "-select_streams", "v:0",
    "-show_entries", "stream=codec_name",
    "-of", "default=noprint_wrappers=1:nokey=1",
]

RTSP_URL = "rtsp://{username}:{password}@{ip}:{port}/cam/realmonitor?channel={channel}&subtype=0"
PROXY_URL = "/camera/image/{ip}/{channel}"