import numpy as np
import torch
import logging
from collections import deque
import time
from enum import Enum
from typing import Dict, List, Callable, Any
from silero_vad.utils_vad import OnnxWrapper
import os

class AudioEventType(Enum):
	SPEECH_START = "speech_start"           # 발화 시작
	SPEECH_DATA = "speech_data"             # 발화 중 오디오 데이터
	SPEECH_END = "speech_end"               # 발화 종료
	BRIEF_SILENCE = "brief_silence"         # 짧은 침묵 감지
	VAD_STATE_CHANGE = "vad_state_change"   # VAD 상태 변경
	ENERGY_LEVEL_UPDATE = "energy_update"   # 에너지 레벨 업데이트
	TEXT_RESULT = "text_result"             # 텍스트 인식 결과
	CONNECTION_STATE = "connection_state"   # 연결 상태 변경
	ERROR = "error"                         # 오류 발생

class AudioEventManager:	
	_instance = None
	_lock = None
	
	@classmethod
	def get_instance(cls):
		"""싱글톤 패턴 인스턴스 접근"""
		import threading
		if cls._lock is None:
			cls._lock = threading.Lock()
			
		with cls._lock:
			if cls._instance is None:
				cls._instance = cls()
			return cls._instance
	
	def __init__(self):
		"""초기화"""
		self._subscribers: Dict[AudioEventType, List[Callable]] = {
			event_type: [] for event_type in AudioEventType
		}
		self._event_data = {}  # 최신 이벤트 데이터 저장
		self._shared_state = {
			'is_speech_active': False,
			'current_energy': 0,
			'accumulated_text': "",
			'is_connected': False,
			'latest_audio_data': None,
			'vad_probability': 0.0
		}
	
	def subscribe(self, event_type: AudioEventType, callback: Callable):
		"""이벤트 구독"""
		if event_type in self._subscribers:
			if callback not in self._subscribers[event_type]:
				self._subscribers[event_type].append(callback)
				return True
		return False
	
	def unsubscribe(self, event_type: AudioEventType, callback: Callable):
		"""이벤트 구독 해제"""
		if event_type in self._subscribers and callback in self._subscribers[event_type]:
			self._subscribers[event_type].remove(callback)
			return True
		return False
	
	def publish(self, event_type: AudioEventType, data=None):
		"""이벤트 발행"""
		if event_type not in self._subscribers:
			return False
		
		# 이벤트 데이터 저장
		self._event_data[event_type] = data
		
		# 공유 상태 업데이트
		self._update_shared_state(event_type, data)
		
		# 구독자들에게 이벤트 전달
		for callback in self._subscribers[event_type]:
			try:
				callback(data)
			except Exception as e:
				logging.error(f"이벤트 처리 중 오류: {e}")
		
		return True
	
	def _update_shared_state(self, event_type: AudioEventType, data):
		"""이벤트 타입에 따라 공유 상태 업데이트"""
		if event_type == AudioEventType.SPEECH_START:
			self._shared_state['is_speech_active'] = True
		elif event_type == AudioEventType.SPEECH_END:
			self._shared_state['is_speech_active'] = False
		elif event_type == AudioEventType.ENERGY_LEVEL_UPDATE and isinstance(data, (int, float)):
			self._shared_state['current_energy'] = data
		elif event_type == AudioEventType.TEXT_RESULT and isinstance(data, str):
			self._shared_state['accumulated_text'] = data
		elif event_type == AudioEventType.CONNECTION_STATE and isinstance(data, bool):
			self._shared_state['is_connected'] = data
		elif event_type == AudioEventType.SPEECH_DATA and isinstance(data, bytes):
			self._shared_state['latest_audio_data'] = data
		elif event_type == AudioEventType.VAD_STATE_CHANGE and isinstance(data, dict):
			if 'probability' in data:
				self._shared_state['vad_probability'] = data['probability']
	
	def get_shared_state(self):
		"""현재 공유 상태 반환"""
		return self._shared_state.copy()
	
	def get_latest_event_data(self, event_type: AudioEventType):
		"""특정 이벤트 타입의 최신 데이터 반환"""
		return self._event_data.get(event_type)

class VoiceDetector:
	"""음성 감지 클래스 - 오디오 데이터에서 음성을 감지하는 기능만 제공"""
	
	def __init__(self, sample_rate=16000, energy_threshold=40, 
				 vad_threshold=0.5, silence_limit=0.3, speech_debounce_time=0.3,
				 min_continuous_speech=0.5, use_event_manager=True,
				 model_reset_interval=5.0, min_buffer_size=1024):
		"""
		음성 감지 클래스 초기화
		
		Args:
			sample_rate: 오디오 샘플링 레이트
			energy_threshold: 에너지 임계값 (로그 스케일 적용)
			vad_threshold: Silero VAD 감지 임계값 (0.0~1.0)
			silence_limit: 침묵으로 간주할 최대 시간(초)
			speech_debounce_time: 디바운싱 시간(초)
			min_continuous_speech: 유효한 음성으로 인식할 최소 지속 시간(초)
			use_event_manager: 이벤트 매니저 사용 여부
			model_reset_interval: 모델 상태 초기화 간격(초)
			min_buffer_size: 오디오 처리에 필요한 최소 버퍼 크기 (샘플 수)
		"""
		# 기본 설정
		self.logger = logging.getLogger('VoiceDetector')
		
		self.sample_rate = sample_rate
		self.energy_threshold = energy_threshold
		self.vad_threshold = vad_threshold
		self.silence_limit = silence_limit
		self.speech_debounce_time = speech_debounce_time
		self.min_continuous_speech = min_continuous_speech
		self.min_buffer_size = min_buffer_size  # 최소 버퍼 크기 (기본값: 512 샘플)
		
		# 에너지 레벨 계산 버퍼
		self.energy_buffer = deque(maxlen=10)
		self.current_energy = 0
		
		# 말하기 상태 관리 변수
		self.is_speaking_now = False
		self.speech_start_time = None
		self.silence_start_time = None
		self.last_state_change = 0
		self.debounce_buffer = []
		
		# 디바운싱용 오디오 버퍼
		self.debounce_audio_buffer = deque(maxlen=int(self.speech_debounce_time * self.sample_rate / 512))
		
		# 짧은 오디오 처리를 위한 버퍼 추가
		self.audio_accumulator = np.array([], dtype=np.int16)
		
		# Silero VAD 모델 로드
		self.model_path = "silero_vad/models/silero_vad_16k_op15.onnx"
		self._load_silero_model()
		
		# 모델 초기화 관련 변수 추가
		self.model_reset_interval = model_reset_interval  # 모델 초기화 간격(초)
		self.last_model_reset_time = time.time()  # 마지막 초기화 시간
		
		# 짧은 침묵 감지 관련 변수
		self.on_brief_silence_detected = None
		self.last_speech_prob = 0
		self.recent_vad_probs = deque(maxlen=5)  # 최근 5개 VAD 확률 저장
		self.brief_silence_start_time = None
		self.last_silence_detection_time = 0
		self.last_human_voice_detection = False
		
		# 이벤트 매니저
		self.use_event_manager = use_event_manager
		if self.use_event_manager:
			self.event_manager = AudioEventManager.get_instance()
		else:
			self.event_manager = None

	def _load_silero_model(self):
		"""Silero VAD ONNX 모델 로드"""
		import requests
		try:
			# 모델 파일이 없으면 다운로드
			if not os.path.exists(self.model_path):
				self.logger.info(f"모델 파일이 없습니다. 다운로드를 시작합니다: {self.model_path}")
				
				# GitHub Raw 링크로 변경
				model_url = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad_16k_op15.onnx"
				
				# Requests를 사용하여 파일 다운로드
				response = requests.get(model_url)
				if response.status_code == 200:
					with open(self.model_path, 'wb') as f:
						f.write(response.content)
					self.logger.info(f"모델 다운로드 완료: {self.model_path}")
				else:
					self.logger.error(f"모델 다운로드 실패: HTTP 상태 코드 {response.status_code}")
					raise Exception(f"모델 다운로드 실패: HTTP 상태 코드 {response.status_code}")
				
			# OnnxWrapper 초기화
			self.model = OnnxWrapper(self.model_path)
			self.logger.info("Silero VAD ONNX 모델 로드 완료")
		except Exception as e:
			self.logger.error(f"모델 로드 실패: {str(e)}")
			raise
	
	def _accumulate_audio(self, audio_data):
		"""
		오디오 데이터를 누적하고 최소 버퍼 크기를 초과하면 처리 가능한 상태를 반환
		
		Args:
			audio_data: 오디오 데이터 (numpy 배열 또는 바이트)
			
		Returns:
			tuple: (처리 가능 여부, 누적된 오디오 데이터)
		"""
		# 오디오 데이터를 numpy 배열로 변환
		if isinstance(audio_data, bytes):
			audio_array = np.frombuffer(audio_data, dtype=np.int16)
		elif isinstance(audio_data, np.ndarray) and audio_data.dtype == np.int16:
			audio_array = audio_data
		elif isinstance(audio_data, np.ndarray) and audio_data.dtype == np.float32:
			audio_array = (audio_data * 32768).astype(np.int16)
		else:
			self.logger.warning(f"지원되지 않는 오디오 데이터 형식: {type(audio_data)}")
			return False, None
		
		# 오디오 데이터 누적
		self.audio_accumulator = np.append(self.audio_accumulator, audio_array)
		
		# 버퍼 크기가 최소 요구 크기를 초과하는지 확인
		can_process = len(self.audio_accumulator) >= self.min_buffer_size
		
		# 처리 가능하면 누적된 오디오 데이터 반환
		if can_process:
			accumulated_audio = self.audio_accumulator.copy()
			
			# 버퍼 초기화 (처리 후 남은 샘플들은 유지)
			remainder = len(self.audio_accumulator) % self.min_buffer_size
			if remainder > 0:
				self.audio_accumulator = self.audio_accumulator[-remainder:]
			else:
				self.audio_accumulator = np.array([], dtype=np.int16)
			
			return True, accumulated_audio
		
		return False, None
	
	def is_human_voice(self, audio_data):
		"""
		오디오 데이터에 사람 음성이 포함되어 있는지만 확인 (에너지 임계치 무시)
		
		Args:
			audio_data: 오디오 데이터 (numpy 배열 또는 바이트)
			
		Returns:
			bool: 사람 음성 감지 여부
		"""
		try:
			# 누적 버퍼에 오디오 추가 및 처리 가능 여부 확인
			can_process, processed_audio = self._accumulate_audio(audio_data)
			
			if not can_process:
				return self.last_human_voice_detection
			
			# print(f"오디오 데이터: 길이={len(processed_audio)}, 최소={np.min(processed_audio)}, 최대={np.max(processed_audio)}, 평균={np.mean(np.abs(processed_audio))}")
			
			# 모델 상태 초기화 간격 확인
			self._check_and_reset_model_states()
			
			# 정규화된 오디오 데이터 생성
			normalized_audio = processed_audio.astype(np.float32) / 32768.0
			
			# Silero VAD 모델 처리
			samples_per_frame = 512
			speech_probs = []
			
			# 프레임 처리
			for i in range(0, len(normalized_audio) - samples_per_frame + 1, samples_per_frame):
				frame = normalized_audio[i:i+samples_per_frame]
				if len(frame) == samples_per_frame:
					frame_tensor = torch.from_numpy(frame)
					speech_prob = self.model(frame_tensor, self.sample_rate).item()
					speech_probs.append(speech_prob)
			
			# 결과 처리
			if speech_probs:
				avg_speech_prob = sum(speech_probs) / len(speech_probs)
				# print(f"VAD 확률: {avg_speech_prob:.4f}, 임계값: {self.vad_threshold:.4f}")
				
				is_speech = avg_speech_prob > self.vad_threshold
				self.last_human_voice_detection = is_speech  # 마지막 음성 감지 결과 저장
				return is_speech
			else:
				print("처리된 프레임 없음")
				return False
				
		except Exception as e:
			import traceback
			print(f"음성 감지 중 오류: {e}")
			print(traceback.format_exc())
			return False
	
	def _check_and_reset_model_states(self):
		"""모델 초기화 간격을 확인하고 필요시 모델 상태 초기화"""
		current_time = time.time()
		if current_time - self.last_model_reset_time >= self.model_reset_interval:
			# Silero VAD 모델은 reset_states 메서드가 없으므로 다시 로드
			self.model.reset_states()
			self.last_model_reset_time = current_time
	
	def calculate_energy(self, audio_data):
		"""
		오디오 데이터의 에너지 레벨 계산 (정규화 및 로그 스케일 적용)
		
		Args:
			audio_data: 바이트 형태의 오디오 데이터
			
		Returns:
			float: 계산된 에너지 레벨
		"""
		if isinstance(audio_data, bytes):
			# 16비트 오디오 데이터를 numpy 배열로 변환
			audio_array = np.frombuffer(audio_data, dtype=np.int16)
			
			# 정규화 (16비트 오디오의 최대값으로 나눔)
			normalized_audio = audio_array.astype(np.float32) / 32768.0
			
			# 에너지 계산 (제곱의 평균)
			energy_raw = np.mean(np.square(normalized_audio))
			
			# 0에 가까운 값에 대한 log 계산 오류 방지를 위해 작은 값 추가
			energy = 10.0 * np.log10(energy_raw + 1e-10) + 100
			
			# 음수 값 방지 (매우 조용한 소리에 대해 음수가 나올 수 있음)
			energy = max(0, energy)
			
			# 현재 에너지 레벨 업데이트
			self.current_energy = energy
			
			# 이벤트 발행 (에너지 레벨 업데이트)
			if self.use_event_manager:
				self.event_manager.publish(AudioEventType.ENERGY_LEVEL_UPDATE, energy)
			
			return energy
		return 0
	
	def detect_voice(self, audio_data):
		"""
		Silero VAD를 사용해 오디오 데이터에 음성이 포함되어 있는지 확인
		
		Args:
			audio_data: 바이트 형태의 오디오 데이터
			
		Returns:
			bool: 음성 감지 여부
		"""
		try:
			# 누적 버퍼에 오디오 추가 및 처리 가능 여부 확인
			can_process, processed_audio = self._accumulate_audio(audio_data)
			
			# 충분한 오디오 데이터가 없으면 이전 결과 반환
			if not can_process:
				return self.is_speaking_now
			
			# 처리된 오디오 데이터를 바이트로 변환
			processed_audio_bytes = processed_audio.tobytes()
			
			# 모델 상태 초기화 간격 확인
			self._check_and_reset_model_states()
			
			# 에너지 레벨 계산
			energy_level = self.calculate_energy(processed_audio_bytes)
			
			# 에너지 버퍼에 추가
			self.energy_buffer.append(energy_level)
			
			# 노이즈 필터링을 위한 평균 에너지 계산
			avg_energy = sum(self.energy_buffer) / len(self.energy_buffer) if self.energy_buffer else energy_level
			
			# 오디오 데이터를 numpy 배열로 변환 후 정규화
			normalized_audio = processed_audio.astype(np.float32) / 32768.0
			
			# Silero VAD 모델은 16kHz에서 정확히 512개 샘플만 처리 가능
			samples_per_frame = 512
			
			is_speech = False
			speech_probs_sum = 0
			frames_count = 0
			
			for i in range(0, len(normalized_audio) - samples_per_frame + 1, samples_per_frame):
				frame = normalized_audio[i:i+samples_per_frame]
				
				# 정확히 512 샘플인 경우에만 처리
				if len(frame) == samples_per_frame:
					frame_tensor = torch.from_numpy(frame)
					speech_prob = self.model(frame_tensor, self.sample_rate).item()
					speech_probs_sum += speech_prob
					frames_count += 1
			
			# 처리된 프레임이 있는 경우 평균 확률 계산
			if frames_count > 0:
				avg_speech_prob = speech_probs_sum / frames_count
				self.last_speech_prob = avg_speech_prob  # 마지막 확률 저장
				is_speech = avg_speech_prob > self.vad_threshold
				
				# VAD 상태 변경 이벤트 발행
				if self.use_event_manager:
					self.event_manager.publish(
						AudioEventType.VAD_STATE_CHANGE, 
						{'probability': avg_speech_prob, 'is_speech': is_speech}
					)
			else:
				# 처리된 프레임이 없으면 이전 상태 유지
				is_speech = self.is_speaking_now
			
			# 에너지 임계치 조건 확인
			energy_condition = avg_energy >= self.energy_threshold
			
			# Silero VAD와 에너지 임계치 모두 고려한 최종 결과
			final_result = is_speech and energy_condition
			
			return final_result
				
		except Exception as e:
			self.logger.error(f"음성 감지 중 오류: {e}")
			# 오류 발생 시 이전 상태 유지
			return self.is_speaking_now
	
	def update_speaking_state(self, is_sound_detected, audio_data):
		"""
		디바운싱을 적용해 말하기 상태 업데이트
		
		Args:
			is_sound_detected: 현재 오디오 데이터에서 음성 감지 여부
			audio_data: 현재 오디오 데이터
			
		Returns:
			bool: 업데이트된 말하기 상태
		"""
		current_time = time.time()
		
		# 오디오 버퍼 초기화 (필요시)
		if not hasattr(self, 'audio_buffer'):
			self.audio_buffer = bytearray()
			self.buffer_is_sound_detected = []
		
		# 오디오 데이터가 bytes 형태인지 확인하고 버퍼에 추가
		if isinstance(audio_data, bytes):
			self.audio_buffer.extend(audio_data)
			self.buffer_is_sound_detected.append(is_sound_detected)
		
		# 버퍼 크기가 충분히 클 때만 처리 (2000바이트 이상)
		MIN_BUFFER_SIZE = 512
		if len(self.audio_buffer) >= MIN_BUFFER_SIZE:
			# 버퍼에 모인 데이터의 음성 감지 결과 계산
			# buffer_is_sound_detected 배열의 평균이나 다수결 사용
			if len(self.buffer_is_sound_detected) > 0:
				buffer_sound_detected = sum(self.buffer_is_sound_detected) / len(self.buffer_is_sound_detected) > 0.5
			else:
				buffer_sound_detected = is_sound_detected
			
			# 디바운싱 버퍼에 현재 상태 추가
			self.debounce_buffer.append((current_time, buffer_sound_detected))
			
			# 기존의 debounce_audio_buffer에 누적된 오디오 데이터 추가
			if hasattr(self, 'debounce_audio_buffer'):
				self.debounce_audio_buffer.append(bytes(self.audio_buffer))
			
			# 버퍼 초기화
			self.audio_buffer = bytearray()
			self.buffer_is_sound_detected = []
		else:
			# 버퍼 크기가 충분하지 않으면 현재 상태만 유지
			return self.is_speaking_now
		
		# speech_debounce_time보다 오래된 데이터 제거
		self.debounce_buffer = [item for item in self.debounce_buffer 
							if current_time - item[0] <= self.speech_debounce_time]
		
		# 버퍼의 상태 비율 계산
		if len(self.debounce_buffer) > 0:
			true_count = sum(1 for _, state in self.debounce_buffer if state)
			true_ratio = true_count / len(self.debounce_buffer)
			
			# 말하기 상태 전환 로직
			prev_state = self.is_speaking_now
			
			# 50% 이상이 음성이면 말하기 상태로 전환
			if true_ratio >= 0.5 and not self.is_speaking_now:
				if self.speech_start_time is None:
					self.speech_start_time = current_time
				
				if current_time - self.last_state_change > (self.speech_debounce_time * 0.5):
					self.is_speaking_now = True
					self.last_state_change = current_time
					self.silence_start_time = None
					
					# 이벤트 발행 (말하기 시작)
					if self.use_event_manager and prev_state != self.is_speaking_now:
						self.event_manager.publish(AudioEventType.SPEECH_START, {
							'time': current_time,
							'energy': self.current_energy
						})
			
			# 20% 이하로 음성이면 침묵 상태로 전환
			elif true_ratio <= 0.2 and self.is_speaking_now:
				if self.silence_start_time is None:
					self.silence_start_time = current_time
				
				if self.silence_start_time and (current_time - self.silence_start_time >= self.silence_limit):
					if current_time - self.last_state_change > (self.speech_debounce_time * 0.5):
						self.is_speaking_now = False
						self.last_state_change = current_time
						self.speech_start_time = None
						
						# 이벤트 발행 (말하기 종료)
						if self.use_event_manager and prev_state != self.is_speaking_now:
							self.event_manager.publish(AudioEventType.SPEECH_END, {
								'time': current_time,
								'duration': current_time - self.last_state_change
							})
			
			# 상태에 따라 타이머 초기화
			if true_ratio >= 0.5:
				self.silence_start_time = None
			elif true_ratio <= 0.3:
				self.speech_start_time = None
		
		return self.is_speaking_now
	
	def process_audio_chunk(self, audio_data):
		"""
		오디오 청크에 대해서 종합적인 음성 감지 및 상태 업데이트 수행
		
		Args:
			audio_data: 바이트 형태의 오디오 데이터
			
		Returns:
			dict: 처리 결과 (음성 감지 여부, 말하기 상태, 에너지 레벨, 짧은 침묵 감지 여부)
		"""
		# 음성 감지
		is_voice_detected = self.detect_voice(audio_data)
		
		# 음성 감지 시 이벤트 발행
		if self.use_event_manager and is_voice_detected:
			self.event_manager.publish(AudioEventType.SPEECH_DATA, audio_data)
		
		# 말하기 상태 업데이트
		is_speaking = self.update_speaking_state(is_voice_detected, audio_data)
		
		# 짧은 침묵 감지 (말하기 중인 경우에만)
		brief_silence_detected = False
		if is_speaking:
			brief_silence_detected = self.detect_brief_silence(audio_data)
		
		# 결과 반환
		return {
			'is_voice_detected': is_voice_detected,
			'is_speaking': is_speaking,
			'energy_level': self.current_energy,
			'brief_silence_detected': brief_silence_detected
		}