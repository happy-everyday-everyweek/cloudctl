# 摄像头声控采集、分片上传与索引

## 1. 声控触发怎么工作

agent 每 block_ms（默认 100）毫秒读一次麦克风电平，算出 RMS 后换成 dBFS。连续超过 threshold_db（默认 -35）达 attack_s（默认 0.3 秒）就认定为开始录像，随后静音持续 hold_s（默认 3 秒）就停。录像期间如果跨过 segment_s（默认 120 秒）会先收尾再开新段，保证单段不会无限长。整段写入 captures/camera/，文件名带摄像头序号与时间戳。

声卡不可用（没装 sounddevice / pyaudio，或被其他程序独占）时不会报错，只是退化为无声控的周期录制，日志里会写明原因。

## 2. 阀值怎么定

先跑 agent --audio-level 5，它会把 5 秒内的平均电平与峰值打出来。安静房间通常落在 -60 到 -45，说话时在 -30 到 -12。把峰值往下调 8 到 10 dB 就是合适阀值，例如峰值 -18 就填 -28。嫌录得多就把阀值往上提，嫌漏录就往下调，同时可以把 attack_s 从 0.3 提到 0.6，避免一声咳嗽就开录。

## 3. 相关参数

capture.camera 下有 enabled、index（第几个摄像头）、fps、width、height、quality、segment_s、min_segment_s（太短的碎片丢弃不传）、max_mb_per_day、active_hours、cooldown_s、audio_track（是否把麦克风声道用 ffmpeg 合进 mp4）。

capture.audio 下有 enabled、device（-1 是默认设备，用 --mic-devices 看编号）、sample_rate、block_ms、threshold_db、attack_s、hold_s、silence_stop、save_audio、max_mb_per_day、active_hours、cooldown_s。

## 4. 绕开 GitHub 单文件限制

单文件超过 upload.max_single_mb（默认 90MB）时不再直接放弃，而是改成切片模式：按 upload.chunk.size_mb（默认 40MB）切成若干 .part 文件，连同 .parts.json 清单一起传到 <原路径>.parts/ 目录下。清单里记着原始文件名、总大小、整体 sha256、每片的偏移与单独 sha256。

想拿回原文件，把 parts 目录和清单下载到本地，然后跑 agent --merge x.parts.json 输出路径，它会按序拼接并校验整体 sha256。自动上传完毕后分片是否保留由 keep_parts 控制。

## 5. 去重与索引

去重两道：StateDB 记每个 sha256 的上传状态（done / exists / chunked / pending / too_large / local_only），local index.db 再按 sha256 与路径各查一次，任何一个命中就跳过。

索引每轮归档结束时刷新到仓库里的 kb/_index/index.json，并按类型额外生成 kind_doc.json、kind_image.json 等。条目含 sha256、仓库路径、字节数、类型、修改时间、来源设备与分片列表。别人 clone 仓库后，只读索引就能检索。本地可以用 agent --index 看统计、agent --index-search 关键词搜、agent --index-build 导出一份到工作目录。

## 6. 命令行速查

agent --cameras 列摄像头；agent --mic-devices 列输入设备；agent --audio-level N 看电平；agent --chunk 文件 --chunk-mb 40 手动切片；agent --merge 清单 输出 还原；agent --index / --index-search / --index-build 看导索引；agent --selftest 一次性自检依赖、静态资源与摄像头。

## 7. 注意

agent 跑在 SYSTEM 上下文时摄像头可能被占用或拿不到用户会话设备，先确认能 --cameras 到索引再开自动录制。声控录制看的是麦克风电平，会议或交谈都会被触发，建议先用 active_hours 和 max_mb_per_day 把范围夹住。摄像头采集只用于你本人拥有或已获授权的设备。
