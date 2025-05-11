import numpy as np
import wave
import tempfile
import os
import time
import threading
from threading import Thread, Event
from queue import Queue
from collections import deque
import logging
import sounddevice as sd
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "../")


from voice_check import VoiceDetector

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AudioRecorder')

class AudioRecorder:
	def __init__(self, voice_detector=None, sample_rate=16000, chunk_size=512,
				 pre_buffer_seconds=1.0, post_buffer_seconds=1.0):
		self.voice_detector = voice_detector
		self.sample_rate = sample_rate
		self.chunk_size = chunk_size
		self.pre_buffer_seconds = pre_buffer_seconds
		self.post_buffer_seconds = post_buffer_seconds
		
		# 녹음 관련 변수
		self.is_recording = False
		self.recording_thread = None
		self.stream = None
		self.audio_buffer = Queue()
		
		# 프리버퍼 설정 (음성 감지 전 오디오 데이터 저장)
		self.pre_buffer_size = int(self.pre_buffer_seconds * self.sample_rate / self.chunk_size)
		self.pre_buffer = deque(maxlen=self.pre_buffer_size)
		
		# 대화 녹음 관련 변수
		self.current_conversation = []
		self.is_conversation_active = False
		self.post_buffer_countdown = 0
		
		# 임시 파일 관리
		self.temp_files = []
		self.temp_file_path = None
		
		# 작업 스레드 관리
		self.action_thread = None
		self.stop_action = Event()
		
	def _input_audio_callback(self, indata, frames, time, status):
		"""sounddevice의 오디오 입력 콜백 함수"""
		if self.is_recording:
			self.audio_buffer.put(bytes(indata))
	
	def start_recording(self):
		"""오디오 녹음 시작"""
		try:
			import sounddevice as sd
			
			self.is_recording = True
			
			# 녹음 스레드 시작
			self.recording_thread = Thread(target=self._recording_thread_function)
			self.recording_thread.daemon = True
			self.recording_thread.start()
			
			# sounddevice 스트림 시작
			self.stream = sd.InputStream(
				callback=self._input_audio_callback,
				channels=1,
				samplerate=self.sample_rate,
				blocksize=self.chunk_size,
				dtype='int16'
			)
			self.stream.start()
			
			logger.info(f"녹음 시작: 샘플 레이트={self.sample_rate}, 청크 크기={self.chunk_size}, " +
					  f"프리버퍼={self.pre_buffer_seconds}초, 포스트버퍼={self.post_buffer_seconds}초")
			return True
		except ImportError:
			logger.error("sounddevice 라이브러리를 설치해주세요: pip install sounddevice")
			return False
		except Exception as e:
			logger.error(f"녹음 시작 중 오류 발생: {e}")
			return False
	
	def stop_recording(self):
		"""녹음 중지"""
		if hasattr(self, 'stream') and self.stream:
			self.stream.stop()
			self.stream.close()
		self.is_recording = False
		if hasattr(self, 'recording_thread') and self.recording_thread.is_alive():
			self.recording_thread.join(timeout=1.0)
			
	def _recording_thread_function(self):
		logger.info("녹음 스레드 시작됨")
		
		while self.is_recording:
			if not self.audio_buffer.empty():
				# 오디오 버퍼에서 데이터 가져오기
				data = self.audio_buffer.get()
				
				# 항상 프리버퍼에 데이터 추가
				self.pre_buffer.append(data)
				
				# 음성 감지 처리 (VoiceDetector가 있는 경우)
				is_speaking = False
				if self.voice_detector:
					result = self.voice_detector.process_audio_chunk(data)
					is_speaking = result['is_speaking']
				
				# 대화 시작 (음성이 감지되고 대화가 아직 활성화되지 않은 경우)
				if is_speaking and not self.is_conversation_active:
					self._start_conversation()
					
				# 포스트 버퍼링 중 음성이 다시 활성화되는 경우 처리
				if self.is_conversation_active and self.post_buffer_countdown > 0 and is_speaking:
					logger.info("포스트 버퍼링 중 음성이 다시 감지되었습니다. 일반 대화 모드로 복귀합니다.")
					self.post_buffer_countdown = 0
				
				# 대화 중이면 데이터 저장
				if self.is_conversation_active:
					self.current_conversation.append(data)
				
				# 포스트 버퍼 처리 (대화가 끝난 후에도 일정 시간 동안 데이터 수집)
				if not is_speaking and self.is_conversation_active:
					self._handle_post_buffer()
			
			time.sleep(0.01)  # CPU 사용률 감소
	
	def _start_conversation(self):
		self.is_conversation_active = True
		self.current_conversation = []
		
		self.stop_action.set()
		logger.info("새로운 음성 감지됨 - 이전 작업 인터럽트 신호 전송")
		
		# 프리버퍼의 데이터를 현재 대화에 추가
		logger.info(f"새로운 대화 시작 - 프리버퍼에서 {len(self.pre_buffer)}개 청크 추가")
		for prebuf_data in self.pre_buffer:
			self.current_conversation.append(prebuf_data)
	
	def _handle_post_buffer(self):
		# 처음 침묵이 감지되면 카운트다운 시작
		if self.post_buffer_countdown == 0:
			self.post_buffer_countdown = int(self.post_buffer_seconds * self.sample_rate / self.chunk_size)
			logger.info(f"포스트 버퍼링 시작: {self.post_buffer_countdown}개 청크")
		else:
			self.post_buffer_countdown -= 1
			
		# 포스트 버퍼링이 완료되면 대화 종료 처리
		if self.post_buffer_countdown <= 0:
			logger.info("대화가 종료되었습니다. (포스트 버퍼링 완료)")
			if len(self.current_conversation) > 0:
				temp_file_path = self.save_temp_voice_file(b''.join(self.current_conversation))
				self.temp_file_path = temp_file_path
				self.start_action_after_speech(temp_file_path)
			
			self.current_conversation = []
			self.is_conversation_active = False
	
	def save_temp_voice_file(self, audio_data):
		if not audio_data:
			return None
			
		temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
		temp_file_path = temp_file.name
		temp_file.close()
		
		with wave.open(temp_file_path, 'wb') as wf:
			wf.setnchannels(1)
			wf.setsampwidth(2)  # 16-bit audio
			wf.setframerate(self.sample_rate)
			wf.writeframes(audio_data)
		
		self.temp_files.append(temp_file_path)
		logger.info(f"임시 WAV 파일 저장됨: {temp_file_path}")
		return temp_file_path
	
	def start_action_after_speech(self, voice_file):
		# 인터럽트 플래그 초기화
		self.stop_action.clear()
		
		# 이전 작업이 아직 실행 중이면 중지
		if self.action_thread and self.action_thread.is_alive():
			logger.info("이전 작업이 아직 실행 중입니다 - 중지 신호 전송")
			self.stop_action.set()
			self.action_thread.join(timeout=1.0)
			
			# 작업이 여전히 종료되지 않았다면 로그 출력
			if self.action_thread.is_alive():
				logger.warning("이전 작업이 1초 내에 종료되지 않았습니다")
			
			# 인터럽트 플래그 다시 초기화
			self.stop_action.clear()
			
		# 새 작업 스레드 시작
		self.action_thread = Thread(target=self._perform_action, args=(voice_file,))
		self.action_thread.daemon = True
		self.action_thread.start()
	
	def _perform_action(self, voice_file):
		logger.info(f"음성 파일 {voice_file}을 처리하는 작업을 시작합니다...")
		
		# 매 작업 단계마다 인터럽트 신호 확인
		for i in range(10):
			# 인터럽트 신호가 있는지 확인
			if self.stop_action.is_set():
				logger.info("작업이 인터럽트되었습니다 - 새 음성이 감지됨")
				return
				
			logger.info(f"작업 진행 중... {i+1}/10")
			time.sleep(0.5)
			
		logger.info("작업이 완료되었습니다.")
		
	def cleanup_temp_files(self):
		for file_path in self.temp_files:
			try:
				if os.path.exists(file_path):
					os.remove(file_path)
					logger.info(f"임시 파일 삭제: {file_path}")
			except Exception as e:
				logger.error(f"파일 삭제 중 오류 발생: {e}")
		self.temp_files = []
	
	def monitor_conversation_loop(self):
		logger.info("대화 모니터링 루프를 시작합니다. 종료하려면 Ctrl+C를 누르세요.")
		
		if not self.start_recording():
			logger.error("녹음을 시작할 수 없습니다.")
			return
			
		try:
			while True:
				time.sleep(0.5)
		except KeyboardInterrupt:
			logger.info("\n프로그램을 종료합니다.")
		finally:
			self.stop_recording()
			self.cleanup_temp_files()
	
	def __del__(self):
		"""객체 소멸 시 임시 파일 정리"""
		self.cleanup_temp_files()


# 사용자 정의 작업 클래스 예시
class CustomAudioRecorder(AudioRecorder):
	def _perform_action(self, voice_file):
		"""사용자 정의 작업 구현"""
		logger.info(f"사용자 정의 작업을 수행합니다: {voice_file}")
		
		# 작업 실행 예시
		num = 0
		while not self.stop_action.is_set() and num < 5:
			logger.info(f"사용자 정의 작업 진행 중... {num+1}/5")
			num += 1
			time.sleep(1)
			
		logger.info("사용자 정의 작업이 완료되었습니다.")
		


def main():
	"""음성 감지 및 오디오 녹음 테스트"""
	
	# VoiceDetector 인스턴스 생성
	detector = VoiceDetector(
		sample_rate=16000,
		energy_threshold=54,            # 에너지 임계치
		vad_threshold=0.1,              # Silero VAD 감지 임계값 (0.0~1.0)
		silence_limit=1.5,              # 침묵 제한 시간(초)
		speech_debounce_time=0.5,       # 디바운스 시간(초)
		min_continuous_speech=1.0,      # 최소 연속 발화 시간(초)
		use_event_manager=True          # 이벤트 매니저 사용
	)
	
	# 짧은 침묵 감지 콜백 등록
	def on_brief_silence(audio_data):
		print("짧은 침묵이 감지되었습니다!")
	
	detector.register_brief_silence_callback(on_brief_silence)
	
	# AudioRecorder 인스턴스 생성
	recorder = CustomAudioRecorder(
		voice_detector=detector,
		sample_rate=16000,
		chunk_size=512,
		pre_buffer_seconds=1.0,        # 음성 감지 전 버퍼링할 시간(초)
		post_buffer_seconds=1.0        # 음성 종료 후 버퍼링할 시간(초)
	)
	
	# 대화 모니터링 루프 시작
	try:
		recorder.monitor_conversation_loop()
	except KeyboardInterrupt:
		print("\n프로그램을 종료합니다.")
	finally:
		# 정리 작업
		recorder.stop_recording()
		recorder.cleanup_temp_files()

if __name__ == "__main__":
	main()