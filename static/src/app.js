import { RTVIClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

const startButton = document.getElementById('startButton');
const stopButton = document.getElementById('stopButton');
const botVideo = document.getElementById('botVideo');
const botAudio = document.getElementById('botAudio');

const sttLatencyEl = document.getElementById('sttLatency');
const llmLatencyEl = document.getElementById('llmLatency');
const ttsLatencyEl = document.getElementById('ttsLatency');
const totalLatencyEl = document.getElementById('totalLatency');

let rtviClient = null;
let remoteVideoTrack = null;
let remoteAudioTracks = [];
let botIsReady = false;
let userIsSpeaking = false;
let botIsSpeaking = false;
let isInterrupted = false;
let interruptTimeout = null;
let firstBotUtteranceCompleted = false; // 첫 번째 봇 발화 완료 여부 추적
let localMicTrack = null; // 로컬 마이크 트랙 참조
let micEnabled = false; // 마이크 활성화 상태


class LatencyTracker {
  constructor() {
    this.timestamps = {
      userStartSpeaking: null,
      userStopSpeaking: null,
      sttComplete: null,
      llmResponseStart: null,
      llmFirstTextReceived: null,
      botStartSpeaking: null
    };
    
    this.latencies = {
      stt: [],
      llm: [],
      tts: [],
      total: []
    };
  }
  
  reset() {
    this.timestamps = {
      userStartSpeaking: null,
      userStopSpeaking: null,
      sttComplete: null,
      llmResponseStart: null,
      llmFirstTextReceived: null,
      botStartSpeaking: null
    };
  }
  
  // 타임스탬프 기록 메서드
  recordUserStartSpeaking() {
    this.timestamps.userStartSpeaking = Date.now();
  }
  
  recordUserStopSpeaking() {
    this.timestamps.userStopSpeaking = Date.now();
  }
  
  // STT 완료 기록 (발화 종료 후 인식 완료까지의 시간)
  recordSTTComplete() {
    this.timestamps.sttComplete = Date.now();
    
    if (this.timestamps.userStopSpeaking) {
      const latency = this.timestamps.sttComplete - this.timestamps.userStopSpeaking;
      this.latencies.stt.push(latency);
      this.updateUI('stt', latency);
      console.log(`📊 STT 레이턴시: ${latency}ms`);
    }
  }
  
  // LLM 시작 시간 기록
  recordLLMStart() {
    this.timestamps.llmResponseStart = Date.now();
  }
  
  // LLM 첫 텍스트 수신 시간 기록
  recordLLMFirstText() {
    if (!this.timestamps.llmFirstTextReceived) {
      this.timestamps.llmFirstTextReceived = Date.now();
      this.calculateLLMLatency();
    }
  }
  
  // 봇 발화 시작 기록
  recordBotStartSpeaking() {
    this.timestamps.botStartSpeaking = Date.now();
    this.calculateTTSLatency();
    this.calculateTotalLatency();
  }
  
  // LLM 레이턴시 계산
  calculateLLMLatency() {
    if (this.timestamps.llmResponseStart && this.timestamps.llmFirstTextReceived) {
      const latency = this.timestamps.llmFirstTextReceived - this.timestamps.llmResponseStart;
      this.latencies.llm.push(latency);
      this.updateUI('llm', latency);
      console.log(`📊 LLM 레이턴시: ${latency}ms`);
    }
  }
  
  // TTS 레이턴시 계산 - LLM 첫 텍스트에서 봇 발화 시작까지
  calculateTTSLatency() {
    if (this.timestamps.llmFirstTextReceived && this.timestamps.botStartSpeaking) {
      const latency = this.timestamps.botStartSpeaking - this.timestamps.llmFirstTextReceived;
      this.latencies.tts.push(latency);
      this.updateUI('tts', latency);
      console.log(`📊 TTS 레이턴시: ${latency}ms`);
    }
  }
  
  // 총 레이턴시 계산
  calculateTotalLatency() {
    if (this.timestamps.userStopSpeaking && this.timestamps.botStartSpeaking) {
      const latency = this.timestamps.botStartSpeaking - this.timestamps.userStopSpeaking;
      this.latencies.total.push(latency);
      this.updateUI('total', latency);
      console.log(`📊 총 레이턴시: ${latency}ms`);
    }
  }
  
  // 평균 계산
  getAverage(type) {
    const values = this.latencies[type];
    if (values.length === 0) return 0;
    return Math.round(values.reduce((a, b) => a + b, 0) / values.length);
  }
  
  // UI 업데이트
  updateUI(type, latency) {
    const average = this.getAverage(type);
    const elementMap = {
      stt: sttLatencyEl,
      llm: llmLatencyEl,
      tts: ttsLatencyEl,
      total: totalLatencyEl
    };
    
    const element = elementMap[type];
    if (element) {
      element.innerHTML = `
        <div class="latency-label">${type.toUpperCase()} 레이턴시</div>
        <div class="latency-current">${latency}ms</div>
        <div class="latency-avg">평균: ${average}ms</div>
      `;
    }
  }
}

// 레이턴시 추적기 인스턴스
const latencyTracker = new LatencyTracker();

// -------------------- 마이크 제어 함수 --------------------

// 마이크 활성화/비활성화 토글 함수
function toggleMicrophone(enable) {
  if (!rtviClient || !localMicTrack) return;
  
  try {
    localMicTrack.enabled = enable;
    micEnabled = enable;
    
    console.log(`마이크 ${enable ? '활성화' : '비활성화'} 완료`);
    
  } catch (error) {
    console.error(`마이크 상태 변경 오류: ${error.message}`);
  }
}

// -------------------- 미디어 설정 및 관리 함수 --------------------

/**
 * 오디오 트랙 설정 함수
 */
function setupAudioTrack(track) {
  console.log(`봇 오디오 트랙 설정 (ID: ${track.id})`);
  
  // 이미 같은 트랙을 사용 중인지 확인
  if (botAudio.srcObject) {
    const oldTrack = botAudio.srcObject.getAudioTracks()[0];
    if (oldTrack?.id === track.id) {
      return;
    }
  }
  
  try {
    const audioStream = new MediaStream([track]);
    
    botAudio.srcObject = audioStream;
    botAudio.muted = false;
    botAudio.volume = 1.0;
    
    if (!remoteAudioTracks.includes(track)) {
      remoteAudioTracks.push(track);
    }
    
    botAudio.play()
      .then(() => {
        console.log('오디오 재생 시작됨');
        botIsSpeaking = true;
      })
      .catch(e => {
        console.error(`오디오 재생 오류: ${e.message}`);
      });
  } catch (err) {
    console.error(`오디오 설정 오류: ${err.message}`);
  }
}

/**
 * 비디오 트랙 설정 함수
 */
function setupVideoTrack(track) {
  console.log(`봇 비디오 트랙 설정 (ID: ${track.id})`);
  
  // 이미 같은 트랙을 사용 중인지 확인
  if (botVideo.srcObject) {
    const oldTrack = botVideo.srcObject.getVideoTracks()[0];
    if (oldTrack?.id === track.id) {
      return;
    }
  }
  
  try {
    // 새 MediaStream 생성하고 트랙 추가
    const videoStream = new MediaStream([track]);
    
    botVideo.srcObject = videoStream;
    botVideo.muted = true;
    
    botVideo.play()
      .then(() => {
        console.log('비디오 재생 시작됨');
        remoteVideoTrack = track;
      })
      .catch(e => {
        console.error(`비디오 재생 오류: ${e.message}`);
      });
  } catch (err) {
    console.error(`비디오 설정 오류: ${err.message}`);
  }
}


function initializeClient() {
  console.log('RTVIClient 초기화 시작');
  
  const transport = new SmallWebRTCTransport({
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "stun:stun1.l.google.com:19302" }
    ],
    debug: true,                // 디버깅 활성화
    videoProcessingEnabled: true // 비디오 처리 명시적 활성화
  });
  
  // RTVIClient 설정 - 마이크 초기에 활성화로 변경
  const rtviClient = new RTVIClient({
    transport,
    enableMic: true,  // 마이크는 활성화되어 있지만 나중에 로컬 트랙을 받으면 비활성화 처리
    enableCam: false,
    enableVideoReceive: true, // 비디오 수신 활성화
    params: {
      baseUrl: "http://localhost:8080", // HTTP로 변경 (HTTPS 아님)
      endpoints: { connect: "/offer" } // /offer 엔드포인트로 변경
    }
  });
  
  // 필수 이벤트만 처리
  rtviClient.on('connected', () => {
    console.log('서버 연결됨');
    stopButton.disabled = false;
  });
  
  rtviClient.on('disconnected', () => {
    console.log('연결 종료됨');
    startButton.disabled = false;
    stopButton.disabled = true;
    botIsReady = false;
    userIsSpeaking = false;
    botIsSpeaking = false;
    firstBotUtteranceCompleted = false;
    localMicTrack = null;
    micEnabled = false;
  });
  
  rtviClient.on('botReady', (botReadyData) => {
    console.log('🤖 봇 준비 완료!');
    botIsReady = true;
  });
  
  rtviClient.on('trackStarted', (track, participant) => {
    console.log(`트랙 시작: kind=${track.kind}, id=${track.id}`);
    
    if (track.kind === 'audio' && participant?.local === true) {
      console.log('로컬 마이크 트랙 수신됨');
      localMicTrack = track;
      
      toggleMicrophone(false);
    }
    // 원격 트랙(봇 오디오/비디오) 처리
    else if (!participant?.local) {
      if (track.kind === 'audio') {
        console.log('봇 오디오 트랙 수신됨');
        setupAudioTrack(track);
      } else if (track.kind === 'video') {
        console.log('봇 비디오 트랙 수신됨');
        setupVideoTrack(track);
      }
    }
  });
  
  rtviClient.on('userStartedSpeaking', (participant) => {
    console.log('🗣️ 사용자 발화 시작!');
    userIsSpeaking = true;
    
    latencyTracker.recordUserStartSpeaking();
    
      stopAllMediaPlayback();
  });
  
  rtviClient.on('userStoppedSpeaking', () => {
    console.log('🤫 사용자 발화 종료!');
    userIsSpeaking = false;
    
    latencyTracker.recordUserStopSpeaking();
  });
  
  rtviClient.on('userTranscript', (data) => {
    if (data.final) {
      latencyTracker.recordSTTComplete();
    }
  });
  
  rtviClient.on('botLlmStarted', () => {
    latencyTracker.recordLLMStart();
  });
  
  rtviClient.on('botLlmText', (data) => {
    latencyTracker.recordLLMFirstText();
  });
  
  rtviClient.on('botStartedSpeaking', () => {
    botIsSpeaking = true;
    
    latencyTracker.recordBotStartSpeaking();
    
    if (isInterrupted) {
      reactivateMediaPlayback();
      isInterrupted = false;
    }
  });
  
  rtviClient.on('botStoppedSpeaking', () => {
    botIsSpeaking = false;
    
    if (!firstBotUtteranceCompleted && localMicTrack) {
      firstBotUtteranceCompleted = true;
      console.log('🎙️ 첫 번째 봇 발화 완료 - 마이크 활성화');
      toggleMicrophone(true);
    }
    
    // 한 턴이 끝났으므로 타임스탬프 리셋
    latencyTracker.reset();
  });
  
  // 긴급 메시지 처리
  rtviClient.on('message', (message) => {
    // 인터럽트 관련 메시지 처리
    if (message.type === 'transport_message_urgent' || 
        message.urgent === true ||
        message.label === 'rtvi-ai') {
      handleUrgentMessage(message);
    }
  });
  
  return rtviClient;
}

function handleUrgentMessage(message) {
  console.log(`⚡ 긴급 메시지 수신`);
  
  // 메시지 데이터 추출
  const data = message.data || message.frame?.data || message.frame?.message?.data;
  
  if (data) {
    // 인터럽트 관련 긴급 메시지 처리
    if (data.type === 'interrupt' || data.interrupt) {
      console.log('🚨 서버로부터 인터럽트 요청 수신');
      stopAllMediaPlayback();
    }
  }
}

// 모든 미디어 재생 중단
function stopAllMediaPlayback() {
  console.log('인터럽트 신호 수신 - 모든 미디어 재생 즉시 중단');
  
  // 오디오 처리
  if (botAudio && botAudio.srcObject) {
    try {
      botAudio.pause();
      
      // 오디오 트랙 비활성화
      botAudio.srcObject.getAudioTracks().forEach(track => {
        track.enabled = false;
      });
    } catch (e) {
      console.error(`오디오 중단 오류: ${e.message}`);
    }
  }
  
  botIsSpeaking = false;
  isInterrupted = true;
  
}

// 미디어 재활성화 함수
function reactivateMediaPlayback() {
  console.log('🔄 미디어 재활성화 시작');
  
  // 오디오 재활성화
  if (botAudio && botAudio.srcObject) {
    try {
      // 트랙 재활성화
      botAudio.srcObject.getAudioTracks().forEach(track => {
        track.enabled = true;
      });
      
      // 재생 재개
      botAudio.play()
        .then(() => console.log('오디오 재생 재개됨'))
        .catch(e => console.error(`오디오 재생 재개 오류: ${e.message}`));
    } catch (e) {
      console.error(`오디오 재활성화 오류: ${e.message}`);
    }
  }
  
  // 비디오 재활성화
  if (botVideo && botVideo.srcObject) {
    try {
      // 트랙 재활성화
      botVideo.srcObject.getVideoTracks().forEach(track => {
        track.enabled = true;
      });
      
      // 재생 재개
      botVideo.play()
        .then(() => console.log('비디오 재생 재개됨'))
        .catch(e => console.error(`비디오 재생 재개 오류: ${e.message}`));
    } catch (e) {
      console.error(`비디오 재활성화 오류: ${e.message}`);
    }
  }
}

// -------------------- 이벤트 핸들러 --------------------

// 연결 시작
async function handleStartConnection() {
  startButton.disabled = true;
  stopButton.disabled = true;
  console.log('연결 시도 중...');
  
  // 상태 초기화
  botIsReady = false;
  remoteVideoTrack = null;
  remoteAudioTracks = [];
  userIsSpeaking = false;
  botIsSpeaking = false;
  isInterrupted = false;
  firstBotUtteranceCompleted = false; // 첫 발화 완료 상태 초기화
  localMicTrack = null; // 로컬 마이크 트랙 참조 초기화
  micEnabled = false; // 마이크 상태 초기화
  
  try {
    // 미디어 요소 초기화
    if (botAudio) {
      botAudio.srcObject = null;
      botAudio.muted = false;
      botAudio.volume = 1.0;
    }
    
    if (botVideo) {
      botVideo.srcObject = null;
      botVideo.muted = true; // 비디오는 음소거 (오디오는 별도 요소에서)
    }
    
    rtviClient = initializeClient();
    await rtviClient.connect();
    console.log('연결 완료');
  } catch (e) {
    console.log(`연결 실패: ${e.message}`);
    startButton.disabled = false;
  }
}

// 연결 종료
async function handleStopConnection() {
  if (rtviClient) {
    stopButton.disabled = true;
    console.log('연결 종료 중...');
    
    // 인터럽트 타이머 정리
    if (interruptTimeout) {
      clearTimeout(interruptTimeout);
      interruptTimeout = null;
    }
    isInterrupted = false;
    
    try {
      await rtviClient.disconnect();
      
      // 오디오 정리
      if (botAudio && botAudio.srcObject) {
        botAudio.srcObject.getTracks().forEach(track => track.stop());
        botAudio.srcObject = null;
        botAudio.pause();
      }
      
      // 비디오 정리
      if (botVideo && botVideo.srcObject) {
        botVideo.srcObject.getTracks().forEach(track => track.stop());
        botVideo.srcObject = null;
        botVideo.pause();
      }
      
      // 상태 변수 초기화
      rtviClient = null;
      remoteVideoTrack = null;
      remoteAudioTracks = [];
      localMicTrack = null;
      botIsReady = false;
      micEnabled = false;
      userIsSpeaking = false;
      botIsSpeaking = false;
      firstBotUtteranceCompleted = false;
      
      console.log('연결 종료 완료');
    } catch (e) {
      console.log(`연결 종료 오류: ${e.message}`);
      startButton.disabled = false;
    }
  } else {
    console.log('활성화된 연결이 없습니다');
    startButton.disabled = false;
    stopButton.disabled = true;
  }
}

// -------------------- 이벤트 리스너 설정 --------------------

// 연결 시작 버튼
startButton.addEventListener('click', handleStartConnection);

// 연결 종료 버튼
stopButton.addEventListener('click', handleStopConnection);

if (botAudio) {
  botAudio.addEventListener('playing', () => {
    console.log('오디오 재생 시작됨');
    botIsSpeaking = true;
  });
  
  botAudio.addEventListener('ended', () => {
    console.log('오디오 재생 종료됨');
    botIsSpeaking = false;
  });
  
  botAudio.addEventListener('error', (e) => {
    console.error(`오디오 재생 오류: ${e.target.error?.message || '알 수 없는 오류'}`);
  });
}

window.addEventListener('DOMContentLoaded', () => {
  console.log('Pipecat RTVI 비디오·음성 클라이언트 초기화 완료');
  
  if (botAudio) {
    botAudio.muted = false;
    botAudio.volume = 1.0;
  }
  
  if (botVideo) {
    botVideo.muted = true;
  }
});

// 페이지 언로드 시 정리
window.addEventListener('beforeunload', () => {
  if (rtviClient) {
    rtviClient.disconnect().catch(() => {});
  }
});