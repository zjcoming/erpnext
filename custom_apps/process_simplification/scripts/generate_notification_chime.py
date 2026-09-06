"""Generate our short, finite two-pair notification chime (no external audio)."""

import math
from pathlib import Path
import struct
import wave


def main():
	rate = 24000
	duration = 1.72
	notes = [(0.0, 880), (0.24, 1174.66), (0.84, 880), (1.08, 1174.66)]
	samples = []
	for index in range(round(rate * duration)):
		time = index / rate
		value = 0.0
		for start, frequency in notes:
			age = time - start
			if 0 <= age < 0.6:
				attack = min(age / 0.012, 1)
				release = min((0.6 - age) / 0.08, 1)
				tone = sum(weight * math.sin(2 * math.pi * frequency * harmonic * age)
					for harmonic, weight in [(1, 1), (2, 0.32), (3, 0.12)])
				value += tone * attack * release * math.exp(-4.5 * age)
		samples.append(value)
	peak = max(abs(value) for value in samples)
	pcm = b"".join(struct.pack("<h", round(value / peak * 0.82 * 32767)) for value in samples)
	output = Path(__file__).resolve().parents[1] / "process_simplification/public/sounds/notification-chime-v1.wav"
	output.parent.mkdir(parents=True, exist_ok=True)
	with wave.open(str(output), "wb") as audio:
		audio.setnchannels(1)
		audio.setsampwidth(2)
		audio.setframerate(rate)
		audio.writeframes(pcm)
	print(f"Generated {output.name}: {duration:.2f}s, mono PCM 24kHz, peak 0.82")


if __name__ == "__main__":
	main()
