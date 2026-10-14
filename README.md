# MultiMediaToolSuite
## Tool for the multimedia that most program is missing, complex, and hard to find!

---

This repository contains the simple python tool that you can use with the popular and commercial license tools that you can use easily (think as the cli, gui wrapper than the xml template found in tool such as Dolby DEE)


## Tools

* stacktraxxcovV2.py: GUI tool to converted the Stem Wav (Multi Track Instrumental Part) to the flac multitrack (mka container) and m4a stem compatible (.stem.m4a container, also support .stem.mp4 if rename from .stem.m4a), auto-detect the track 0 combined and auto-combined from the other track if track 0 is absent.
* cutoffsimul.py: GUI tool to simulate the cutoff with the wide range of cutoff hill (contary to brickwall cutoff). This tool might be useful for the audio lossy codec that give bitrate "too much" for the high frequency above most of the 95% human cannot hear (>16-18.5Khz)


---

* VideoConV2SW.py: GUI tools to converted to H265 via x265 predefine 5 profiles including movies, video tape and animate. This tool also converted SRT subtitle that in embedded in the mkv into the tx3g (non-quicktime compliance in this version) and Dolby AC3, Opus, E-AC3, AAC 5.1 Audio into DPL2 (non-compliance Dolby) AAC at the choosable bitrate, also supported opus 5.1 embedded into mp4

* EditChapV2.py: Simple GUI tool to edit chapter data in the mp4 files.
