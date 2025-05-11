#!/usr/bin/env python3
import lightning_whisper_mlx
from lightning_whisper_mlx.transcribe import transcribe_audio
import numpy as np
import time
import sys
import os
from colorama import Fore, Style, init as colorama_init
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../')))
from vad.voice_check import VoiceDetector
import librosa

# 몽키 패치 initial_prompt 추가
def transcribe(self, audio, language=None, initial_prompt=None):
    result = transcribe_audio(audio, path_or_hf_repo=f'./mlx_models/{self.name}', language=language, batch_size=self.batch_size, initial_prompt=initial_prompt)
    return result

lightning_whisper_mlx.LightningWhisperMLX.transcribe = transcribe

try:
    import kss
except ImportError:
    kss = None

# 한국어 토크나이저 클래스 구현
class KoreanTokenizer:
    """한국어 문장 분리를 위한 토크나이저"""
    
    def __init__(self):
        pass    
    def split(self, text):
        """텍스트를 문장 단위로 분리"""
        if kss is None:
            # kss가 없는 경우 기본 구두점 기반 분리
            import re
            sentences = re.split(r'(?<=[.!?])\s+', text)
            return [s.strip() for s in sentences if s.strip()]
        else:
            # kss를 사용한 한국어 문장 분리
            try:
                return kss.split_sentences(text)
            except Exception as e:

                import re
                sentences = re.split(r'(?<=[.!?])\s+', text)
                return [s.strip() for s in sentences if s.strip()]

class HypothesisBuffer:
    """연속 업데이트 간의 텍스트 안정성을 보장하는 버퍼"""
    
    def __init__(self, logfile=sys.stdout):
        self.buffer = []  # 이전 인식 결과 저장
        self.new = []     # 새로운 인식 결과 저장
        self.commited_in_buffer = []  # 확정된 텍스트 저장
        self.provisional_buffer = []  # 빠른 업데이트를 위한 임시 버퍼
        self.last_commited_time = 0
        self.last_commited_word = None
        self.logfile = logfile
    
    def insert(self, words, offset):
        """새 단어 목록을 버퍼에 삽입하고 중복을 제거"""
        new_words = []
        
        # 사전 형식 처리
        if words and isinstance(words[0], dict):
            for word_data in words:
                text = word_data["text"]
                t0, t1 = word_data.get("timestamp", (0, 0))
                t0 += offset
                t1 += offset
                new_words.append((t0, t1, text))
        else:
            print(f"경고: 예상치 못한 입력 형식 - {words}", file=self.logfile)
            return
        
        # 확정 시간 이후의 단어만 선택
        self.new = [(a, b, t) for a, b, t in new_words if a > self.last_commited_time - 0.1]
        
        # n-gram 매칭으로 중복 제거
        if len(self.new) >= 1 and self.commited_in_buffer:
            a, b, t = self.new[0]
            
            if abs(a - self.last_commited_time) < 1:
                cn = len(self.commited_in_buffer)
                nn = len(self.new)
                
                for i in range(1, min(min(cn, nn), 5) + 1):
                    c = " ".join([self.commited_in_buffer[-j][2] for j in range(1, i+1)][::-1])
                    tail = " ".join(self.new[j-1][2] for j in range(1, i+1))
                    
                    if c == tail:
                        for j in range(i):
                            self.new.pop(0)
                        break
    
    def flush(self, is_final=False):
        """버퍼 일치 항목 확정 및 반환"""
        commit = []
        
        if is_final:
            # 일치 항목 처리
            while self.new and self.buffer:
                na, nb, nt = self.new[0]
                ba, bb, bt = self.buffer[0]
                
                if nt == bt:
                    commit.append((na, nb, nt))
                    self.last_commited_word = nt
                    self.last_commited_time = nb
                    self.buffer.pop(0)
                    self.new.pop(0)
                else:
                    break
            
            # 남은 버퍼 전체 확정
            if self.buffer:
                remaining = self.buffer.copy()
                commit.extend(remaining)
                
                if remaining:
                    self.last_commited_word = remaining[-1][2]
                    self.last_commited_time = remaining[-1][1]
            
            # 남은 새 항목 전체 확정
            if self.new:
                remaining = self.new.copy()
                commit.extend(remaining)
                
                if remaining:
                    self.last_commited_word = remaining[-1][2]
                    self.last_commited_time = remaining[-1][1]
            
            self.buffer = []
            self.new = []
        else:
            # 일반적인 경우(발화 중)
            while self.new and self.buffer:
                na, nb, nt = self.new[0]
                ba, bb, bt = self.buffer[0]
                
                if nt == bt:
                    commit.append((na, nb, nt))
                    self.last_commited_word = nt
                    self.last_commited_time = nb
                    self.buffer.pop(0)
                    self.new.pop(0)
                else:
                    break
            
            self.buffer = self.new.copy()
            self.new = []
        
        self.commited_in_buffer.extend(commit)
        return commit
    
    def clear(self):
        """버퍼 초기화"""
        self.commited_in_buffer = []
        self.buffer = []
        self.new = []
        self.last_commited_time = 0
        self.last_commited_word = None

    def pop_commited(self, time):
        """특정 시간 이전 확정 단어 제거"""
        while self.commited_in_buffer and self.commited_in_buffer[0][1] <= time:
            self.commited_in_buffer.pop(0)

    def get_stable_text(self):
        """안정적으로 인식된 텍스트 반환"""
        return " ".join([t for _, _, t in self.buffer])

    def get_committed_text(self):
        """확정된 텍스트 반환"""
        return " ".join([t for _, _, t in self.commited_in_buffer])

    def complete(self):
        """현재 버퍼 전체 내용 반환"""
        return self.buffer
        
    def get_provisional_text(self):
        """임시 텍스트 반환"""
        if not self.commited_in_buffer:
            return " ".join([t for _, _, t in self.provisional_buffer])
        
        provisional_text = " ".join([t for _, _, t in self.provisional_buffer])
        committed_text = " ".join([t for _, _, t in self.commited_in_buffer])
        
        provisional_text = self._remove_internal_repetitions(provisional_text)
        
        if committed_text and provisional_text:
            return f"{committed_text} {provisional_text}"
        elif committed_text:
            return committed_text
        else:
            return provisional_text

    def _remove_internal_repetitions(self, text):
        """텍스트 내부 반복 패턴 제거"""
        words = text.split()
        if len(words) < 3:
            return text
            
        # 반복 패턴 감지(3-8단어 길이)
        for pattern_len in range(3, min(9, len(words) // 2 + 1)):
            i = 0
            while i <= len(words) - pattern_len * 2:
                pattern = ' '.join(words[i:i+pattern_len])
                next_chunk = ' '.join(words[i+pattern_len:i+pattern_len*2])
                
                if pattern == next_chunk:
                    # 반복 패턴 발견 시 첫 패턴만 유지
                    print(f"내부 반복 패턴 발견: '{pattern}'", file=sys.stdout)
                    return ' '.join(words[:i+pattern_len])
                i += 1
                
        return text

class OnlineSTTProcessor:
    """실시간 음성 인식 처리기"""

    def __init__(self, lightning_whisper, buffer_seconds=60.0, tokenizer=None, min_words_confidence=2,
                 log_level="info", output_mode="incremental"):
        self.whisper = lightning_whisper
        self.audio_buffer = np.array([], dtype=np.float32)
        self.buffer_time_offset = 0.0
        self.sample_rate = 16000
        self.max_buffer_size = int(buffer_seconds * self.sample_rate)

        # 인식 결과 관리
        self.transcript_buffer = HypothesisBuffer()
        self.confirmed_text = []
        self.complete_text = ""
        
        # 토크나이저
        self.tokenizer = tokenizer if tokenizer is not None else KoreanTokenizer()
        self.min_words_confidence = min_words_confidence

        # 타이밍 및 통계
        self.process_times = []
        self.last_chunk_received_time = None
        self.process_start_time = None
        self.latency_stats = {"total": [], "processing": [], "waiting": []}

        # VAD 관련
        self.vad = VoiceDetector(vad_threshold=0.7)
        self.voice_timeout = 0.4  # 발화 종료 감지 시간(초)
        self.voice_active = False  # 현재 발화 중인지 여부
        self.last_voice_activity = 0  # 마지막 음성 활동 시간
        self.silence_duration = 0  # 현재 무음 지속 시간
        self.quick_termination_threshold = 0.3  # 잡음 오발화 빠른 종료 기준(초)
        self.utterance_start_real_time = 0  # 실제 시스템 시간 기준 발화 시작 시간
        
        # 발화 세션 관리
        self.utterance_in_progress = False  # 현재 발화 진행 중 여부
        self.utterance_buffer = np.array([], dtype=np.float32)  # 현재 발화 버퍼
        self.utterance_start_time = 0  # 현재 발화 시작 시간
        
        # 상태 추적
        self.utterance_count = 0
        self.last_result_text = ""  # 마지막 인식 결과
        self.last_interim_time = 0  # 마지막 중간 결과 시간
        self.interim_update_interval = 0.7  # 중간 결과 업데이트 간격(초)

        self.output_mode = output_mode
        colorama_init()

    def init(self):
        """세션 재시작 시 내부 상태 초기화"""
        self.audio_buffer = np.array([], dtype=np.float32)
        self.buffer_time_offset = 0.0
        self.transcript_buffer = HypothesisBuffer()
        self.confirmed_text = []
        self.complete_text = ""
        self.process_times = []
        self.last_chunk_received_time = None
        self.process_start_time = None
        self.latency_stats = {"total": [], "processing": [], "waiting": []}
        self.voice_active = False
        self.last_voice_activity = 0
        self.silence_duration = 0
        self.utterance_in_progress = False
        self.utterance_buffer = np.array([], dtype=np.float32)
        self.utterance_start_time = 0
        self.utterance_start_real_time = 0
        self.utterance_count = 0
        self.last_result_text = ""
        self.last_interim_time = 0
        self.warmup()

    def warmup(self):
        """모델 워밍업"""
        if self.whisper:
            audio, _ = librosa.load("/Users/seodong-gyun/projects/needit/pipecat/warmingup.wav",
                                    sr=self.sample_rate)
            result = self.whisper.transcribe(audio, language="ko")
            print(f"{Fore.GREEN}모델 워밍업 완료{Style.RESET_ALL}")

    def _is_filtered_text(self, text):
        """필터링 대상 텍스트인지 확인"""
        if not text:
            return False
            
        if text.strip() == "!":
            return True
            
        # 같은 글자가 비정상적으로 반복되는 경우
        for char in "아으음애에오우으응헐네":
            if char * 5 in text:
                return True
                
        # 자음 연속 반복
        for char in "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ":
            if char * 5 in text:
                return True
        for char in "가나다라마바사아자차카타파하으":
            if char * 5 in text:
                return True
                
        # 숫자가 많이 포함된 경우
        digit_count = sum(c.isdigit() for c in text)
        if digit_count > len(text) * 0.5 and len(text) > 5:
            return True
            
        # MBC 키워드가 포함된 경우
        if "MBC" in text.upper():
            return True
        
        # 특정 패턴 반복
        if "안녕하세요. 여러분" * 5 in text.strip():
            return True
            
        return False

    def _terminate_utterance(self, reason=""):
        """발화 즉시 종료 처리"""
        self.voice_active = False
        self.utterance_in_progress = False
        self.transcript_buffer.clear()
        self.utterance_buffer = np.array([], dtype=np.float32)
        if reason:
            print(f"{Fore.RED}발화 종료: {reason}{Style.RESET_ALL}")
        return None

    def insert_audio_chunk(self, audio, timestamp=None):
        """오디오 청크 삽입 및 VAD 처리"""
        current_time = time.time()
        if timestamp is not None:
            self.last_chunk_received_time = timestamp
        
        # 전체 버퍼에 추가
        self.audio_buffer = np.append(self.audio_buffer, audio)
        
        # 음성 검출 (VAD)
        is_voice = self.vad.is_human_voice(audio)
        
        # 음성 활동 상태 업데이트
        if is_voice:
            if not self.voice_active:
                print(f"{Fore.GREEN}음성 시작 감지{Style.RESET_ALL}")
                
            self.voice_active = True
            self.last_voice_activity = current_time
            self.silence_duration = 0
            
            # 새 발화 시작
            if not self.utterance_in_progress:
                self.utterance_in_progress = True
                self.utterance_start_time = self.buffer_time_offset + (len(self.audio_buffer) - len(audio)) / self.sample_rate
                self.utterance_start_real_time = current_time
                
                # 발화 시작 시 이전 0.3초 버퍼 포함
                pre_buffer_seconds = 0.3
                pre_buffer_samples = int(pre_buffer_seconds * self.sample_rate)
                current_pos = len(self.audio_buffer) - len(audio)
                start_idx = max(0, current_pos - pre_buffer_samples)
                
                # 이전 버퍼 + 현재 청크로 utterance_buffer 초기화
                self.utterance_buffer = self.audio_buffer[start_idx:].copy()
                
                # 시작 시간 조정
                self.utterance_start_time -= (current_pos - start_idx) / self.sample_rate
                
                self.utterance_count += 1
                self.transcript_buffer.clear()
                self.last_result_text = ""
                print(f"{Fore.CYAN}새 발화 #{self.utterance_count} 시작{Style.RESET_ALL}")
            else:
                # 발화 중에는 현재 청크만 추가
                self.utterance_buffer = np.append(self.utterance_buffer, audio)
            
        else:  # 음성이 아닌 경우
            # 이전에 음성이 있었다면 무음 시간 계산
            if self.voice_active:
                self.silence_duration = current_time - self.last_voice_activity
                
                # 외부 소음에 의한 오발화 빠른 감지
                utterance_duration = current_time - self.utterance_start_real_time
                if self.utterance_in_progress and utterance_duration < self.quick_termination_threshold:
                    return self._terminate_utterance("잡음으로 인한 오발화 감지")
                
                # 일정 시간 이상 무음이 지속되면 발화 종료로 판단
                elif self.silence_duration > self.voice_timeout and self.utterance_in_progress:
                    print(f"{Fore.YELLOW}발화 종료 감지 (무음 {self.silence_duration:.1f}초){Style.RESET_ALL}")
                    self.voice_active = False
                    
                    # 발화 종료 처리
                    if len(self.utterance_buffer) > 0:
                        result = self.complete_utterance()
                        self.utterance_in_progress = False
                        self.transcript_buffer.clear()
                        
                        # 특수 결과 처리
                        if result and result.get("type") in ["short", "filtered", "empty", "invalid"]:
                            return {
                                "type": "final",
                                "start_time": result["start_time"],
                                "end_time": result["end_time"],
                                "text": "",
                                "latency": []
                            }
                        return result
                
                # 무음이지만 아직 발화 종료로 판단하지 않는 경우
                elif self.utterance_in_progress:
                    self.utterance_buffer = np.append(self.utterance_buffer, audio)
        
        # 최대 버퍼 크기 초과 시 안전장치
        if len(self.audio_buffer) > self.max_buffer_size:
            excess = len(self.audio_buffer) - self.max_buffer_size
            self.audio_buffer = self.audio_buffer[excess:]
            self.buffer_time_offset += excess / self.sample_rate
        return None

    def _extract_words_with_timestamps(self, result):
        """Whisper 결과에서 단어+타임스탬프 리스트 추출"""
        words = []
        if isinstance(result, dict):
            # 세그먼트 정보가 있는 경우
            if "segments" in result and len(result["segments"]) > 0:
                for seg in result.get("segments", []):
                    if isinstance(seg, list) and len(seg) >= 3:
                        s, e, txt = seg
                        start = s/1000 if s > 100 else s
                        end = e/1000 if e > 100 else e
                        words.append({"text": txt.strip(), "timestamp": (start, end)})
            # 텍스트만 있는 경우
            elif "text" in result:
                text = result["text"].strip()
                word_list = text.split()
                total_len = len(word_list)
                
                if total_len > 0:
                    duration = 10.0 / total_len
                    for i, word in enumerate(word_list):
                        start_time = i * duration
                        end_time = (i + 1) * duration
                        words.append({"text": word, "timestamp": (start_time, end_time)})
        return words
    
    def process_iter(self):
        """현재 상태 처리 및 중간 결과 반환"""
        current_time = time.time()
        
        # 충분한 오디오가 없으면 처리하지 않음
        if len(self.audio_buffer) < self.sample_rate:
            return (None, None, "", None)
        
        # 발화 중이고 충분한 시간이 지났다면 중간 결과 업데이트
        if self.utterance_in_progress and current_time - self.last_interim_time > self.interim_update_interval:
            self.last_interim_time = current_time
            
            # 중간 인식 수행
            interim_result = self.whisper.transcribe(self.utterance_buffer, language="ko")
            
            if isinstance(interim_result, dict) and 'text' in interim_result and interim_result['text'].strip():
                interim_text = interim_result['text'].strip()
                
                # 중복 업데이트 방지
                if interim_text != self.last_result_text:
                    self.last_result_text = interim_text
                    print(f"{Fore.CYAN}중간 인식: {interim_text}{Style.RESET_ALL}")
                    
                    words_with_timestamps = self._extract_words_with_timestamps(interim_result)
                    
                    if words_with_timestamps:
                        self.transcript_buffer.insert(words_with_timestamps, self.utterance_start_time)
                        provisional_text = self.transcript_buffer.get_provisional_text()
                        return (None, None, provisional_text, {
                            "type": "interim",
                            "chunk_time": self.utterance_start_time
                        })
        
        return (None, None, "", None)

    def complete_utterance(self):
        """발화 완료 처리 - 최종 인식 결과 생성"""
        if len(self.utterance_buffer) < self.sample_rate * 0.5:
            print(f"{Fore.YELLOW}발화가 너무 짧음 ({len(self.utterance_buffer)/self.sample_rate:.1f}초){Style.RESET_ALL}")
            return {
                "type": "short",
                "start_time": self.utterance_start_time,
                "end_time": self.utterance_start_time + len(self.utterance_buffer) / self.sample_rate,
                "text": "",
                "latency": []
            }
        
        self.process_start_time = time.time()
        
        # 최종 인식 수행
        print(f"{Fore.CYAN}최종 인식 중... ({len(self.utterance_buffer)/self.sample_rate:.1f}초){Style.RESET_ALL}")
        
        # 프롬프트 생성
        prompt = self.create_transcript_prompt()
        
        # 최종 인식
        result = self.whisper.transcribe(self.utterance_buffer, language="ko")
        print(f"{Fore.GREEN}최종 인식 결과: {result['text'] if isinstance(result, dict) and 'text' in result else '없음'}{Style.RESET_ALL}")
        
        # 빈 텍스트 결과 처리
        if isinstance(result, dict) and (not 'text' in result or not result['text'].strip()):
            return {
                "type": "empty",
                "start_time": self.utterance_start_time,
                "end_time": self.utterance_start_time + len(self.utterance_buffer) / self.sample_rate,
                "text": "",
                "latency": []
            }
        
        # 유효한 텍스트 결과 처리
        if isinstance(result, dict) and 'text' in result and result['text'].strip():
            text = result['text'].strip()
            
            # 필터링 대상 텍스트 감지
            if self._is_filtered_text(text):
                return {
                    "type": "filtered",
                    "start_time": self.utterance_start_time,
                    "end_time": self.utterance_start_time + len(self.utterance_buffer) / self.sample_rate,
                    "text": "",
                    "filtered_text": text,
                    "latency": []
                }
            
            # 단어 타임스탬프 추출
            words_with_timestamps = self._extract_words_with_timestamps(result)
            
            # 단어 삽입 및 최종 처리
            if words_with_timestamps:
                self.transcript_buffer.insert(words_with_timestamps, self.utterance_start_time)
                self.transcript_buffer.flush(is_final=True)
            
            # 발화 시간 계산
            end_time = self.utterance_start_time + len(self.utterance_buffer) / self.sample_rate
            
            # 결과 추가
            self.confirmed_text.append((self.utterance_start_time, end_time, text))
            self.complete_text = " ".join([t for _, _, t in self.confirmed_text])
            
            # 오디오 버퍼 처리
            self.flush_processed_audio(end_time)
            
            # 결과 반환
            final_text = self.transcript_buffer.get_committed_text()
            self.transcript_buffer.flush(is_final=True)
            return {
                "type": "final",
                "start_time": self.utterance_start_time,
                "end_time": end_time,
                "text": final_text,
                "latency": []
            }
        else:
            return {
                "type": "invalid",
                "start_time": self.utterance_start_time,
                "end_time": self.utterance_start_time + len(self.utterance_buffer) / self.sample_rate,
                "text": "",
                "latency": []
            }

    def to_flush(self, segments):
        """세그먼트 목록을 단일 결과로 변환"""
        if not segments:
            return (None, None, "")
        b = segments[0][0]
        e = segments[-1][1]
        txt = " ".join(w for _, _, w in segments)
        return (b, e, txt)

    def create_transcript_prompt(self):
        """현재까지 확인된 텍스트를 기반으로 프롬프트 생성"""
        prompt, length = [], 0
        for _, _, t in reversed(self.confirmed_text):
            prompt.insert(0, t)
            length += len(t) + 1
            if length >= 200:
                break
        return " ".join(prompt)
        
    def flush_processed_audio(self, end_time):
        """처리 완료된 오디오 제거"""
        self.buffer_time_offset += len(self.audio_buffer) / self.sample_rate
        self.audio_buffer = np.array([], dtype=np.float32)
        print(f"{Fore.BLUE}오디오 버퍼 비움 (오프셋: {self.buffer_time_offset:.1f}초){Style.RESET_ALL}")

    def finish(self):
        """세션 종료 - 최종 텍스트 반환"""
        if self.utterance_in_progress and len(self.utterance_buffer) > 0:
            result = self.complete_utterance()
            self.transcript_buffer.clear()
            
            if result and result.get("type") in ["short", "filtered", "empty", "invalid"]:
                b = self.confirmed_text[0][0] if self.confirmed_text else None
                e = result["end_time"]
                return (b, e, "", {})
        
        if not self.confirmed_text:
            return (None, None, "", {})
        
        b = self.confirmed_text[0][0]
        e = self.confirmed_text[-1][1]
        txt = " ".join(w for _, _, w in self.confirmed_text)
        
        print(f"{Fore.GREEN}=== 세션 종료 === 총 {len(self.confirmed_text)}개 발화 처리 완료{Style.RESET_ALL}")
        
        return (b, e, txt, {})