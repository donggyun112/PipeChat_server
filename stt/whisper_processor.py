import os
import uuid
import time
import logging
import librosa
from aiortc import RTCPeerConnection, RTCSessionDescription

import lightning_whisper_mlx
from stt.light_whisper_streaming import OnlineSTTProcessor, KoreanTokenizer
from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
import datetime
import numpy as np
logger = logging.getLogger(__name__)


class WhisperProcessor(FrameProcessor):
	"""
	Whisper 모델을 사용하는 프레임 프로세서
	"""
	def __init__(self, model_name="small", buffer_seconds=15.0, 
				 buffer_trimming="sentence"):
		super().__init__(name="WhisperSTT")
		self.model_name = model_name
		self.buffer_trimming = buffer_trimming
		self.model = None
		self.stt_processor = None
		self.user_id = f"whisper-{uuid.uuid4()}"  # 고유 사용자 ID 생성
		self.buffer_seconds = buffer_seconds
		self.init_model()
		
	def init_model(self):
		"""Whisper 모델 초기화"""
		logger.info(f"Lightning Whisper MLX 모델 로드 중 ({self.model_name})...")
		start_time = time.time()
		self.model = lightning_whisper_mlx.LightningWhisperMLX(model=self.model_name)
		logger.info(f"모델 로드 완료: {time.time() - start_time:.2f}초")
		
		self.stt_processor = OnlineSTTProcessor(
			lightning_whisper=self.model,
			buffer_seconds=self.buffer_seconds,
			tokenizer=KoreanTokenizer(),
		)
		self.stt_processor.init()
	
	async def process_frame(self, frame, direction: FrameDirection):
		await super().process_frame(frame, direction)

		if not isinstance(frame, AudioRawFrame):
			return await self.push_frame(frame, direction)

		pcm_f32 = (np.frombuffer(frame.audio, np.int16)
				.astype(np.float32, copy=False) / 32768.0)

		final_res = self.stt_processor.insert_audio_chunk(pcm_f32, time.time())
		if isinstance(final_res, dict):
			text = final_res["text"]
			if text:
				logger.info(f"WhisperSTT 최종 인식: {text}")
				trans = TranscriptionFrame(
					text=text,
					language="ko-KR",
					user_id=self.user_id,
					timestamp=str(datetime.datetime.now().timestamp())
				)
				await self.push_frame(trans, FrameDirection.DOWNSTREAM)
			return

		start, end, interim_text, meta = self.stt_processor.process_iter()
