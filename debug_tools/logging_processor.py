import datetime
import numpy as np
from loguru import logger

from pipecat.frames.frames import (
	InterimTranscriptionFrame,
	TranscriptionFrame,
	LLMMessagesFrame,
	LLMTextFrame,
	LLMFullResponseStartFrame,
	TextFrame,
)
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContextFrame
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

class LoggingProcessor(FrameProcessor):
	def __init__(self):
		super().__init__(name="Logger")
		self._initialize_queues()
		logger.info("LoggingProcessor initialized")
	
	def _initialize_queues(self):
		"""필요한 큐 속성 초기화"""
		if not hasattr(self, '_FrameProcessor__input_queue'):
			from asyncio import Queue
			self._FrameProcessor__input_queue = Queue()
		if not hasattr(self, '_FrameProcessor__output_queue'):
			from asyncio import Queue
			self._FrameProcessor__output_queue = Queue()

	async def process_frame(self, frame, direction: FrameDirection):
		try:
			logger.debug(f"{self.name}: Processing frame {type(frame).__name__}")
			await super().process_frame(frame, direction)

			if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
				lang = getattr(frame.language, 'value', frame.language)
				txt = getattr(frame, 'text', '')
				logger.info(f"{self.name}: STT result → '{txt}', lang={lang}")

			elif isinstance(frame, LLMMessagesFrame):
				for msg in frame.messages:
					role = msg.get('role')
					content = (msg.get('content','') or '').replace("\n", " ")
					preview = content if len(content)<=100 else content[:100]+'…'
					logger.info(f"{self.name}: LLM {role} → {preview}")

			elif isinstance(frame, OpenAILLMContextFrame):
				msgs = getattr(frame.context, 'messages', None)
				if msgs:
					latest = msgs[-1]
					if isinstance(latest, dict):
						role = latest.get('role','unknown')
						c = latest.get('content','')
					else:
						role = getattr(latest,'role','unknown')
						c = getattr(latest,'content','')
					if not isinstance(c,str):
						parts = getattr(c,'parts', c if isinstance(c,list) else [])
						texts = [p.get('text','') if isinstance(p,dict) else getattr(p,'text','') for p in parts]
						content = ' '.join(filter(None,texts))
					else:
						content = c
					preview = content if len(content)<=100 else content[:100]+'…'
					logger.info(f"{self.name}: Context {role} → {preview}")

			elif isinstance(frame, LLMFullResponseStartFrame):
				logger.info(f"{self.name}: LLM response start")

			elif isinstance(frame, (LLMTextFrame, TextFrame)):
				txt = (getattr(frame,'text','') or '').replace("\n"," ")
				preview = txt if len(txt)<=100 else txt[:100]+'…'
				logger.info(f"{self.name}: Assistant chunk → {preview}")

			await self.push_frame(frame, direction)
		
		except Exception as e:
			try:
				await self.push_frame(frame, direction)
			except Exception:
				logger.error(f"{self.name}: Failed to push frame: {e}")