import asyncio
import os
import uuid
from typing import Dict, Optional, Tuple
from contextlib import asynccontextmanager

import numpy as np
import uvicorn
from fastapi import FastAPI, BackgroundTasks, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from pipecat.frames.frames import OutputImageRawFrame
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.audio.vad.silero import VADParams
from pipecat.pipeline.task import PipelineTask
from pipecat.services.simli.video import SimliVideoService
from pipecat.audio.filters.noisereduce_filter import NoisereduceFilter
from vad.vad_analyze import CustomVADAnalyzer

from run_bot import run_bot


# SimliVideoService의 비디오 처리 메서드 재정의
async def _consume_and_process_video(self):
    await self._pipecat_resampler_event.wait()
    async for video_frame in self._simli_client.getVideoStreamIterator(targetFormat="yuv420p"):
        # 비디오 프레임에서 YUV 평면 추출
        y_plane = np.frombuffer(video_frame.planes[0], dtype=np.uint8).reshape(video_frame.height, video_frame.width)
        u_plane = np.frombuffer(video_frame.planes[1], dtype=np.uint8).reshape(video_frame.height // 2, video_frame.width // 2)
        v_plane = np.frombuffer(video_frame.planes[2], dtype=np.uint8).reshape(video_frame.height // 2, video_frame.width // 2)
        
        # U와 V 평면을 Y 평면 크기에 맞게 업샘플링
        u_resized = np.repeat(np.repeat(u_plane, 2, axis=0), 2, axis=1)
        v_resized = np.repeat(np.repeat(v_plane, 2, axis=0), 2, axis=1)
        
        # BT.709 YUV->RGB 변환 행렬 적용
        # Y = 0~255, U,V = 0~255 (실제로는 16~235, 16~240 범위지만 간단히 구현)
        y = y_plane.astype(np.float32)
        u = u_resized.astype(np.float32) - 128.0  # U 중심점 이동
        v = v_resized.astype(np.float32) - 128.0  # V 중심점 이동
        
        # RGB 변환 (BT.709 공식)
        r = y + 1.5748 * v
        g = y - 0.1873 * u - 0.4681 * v
        b = y + 1.8556 * u
        
        # 값 범위 제한 (0-255)
        r = np.clip(r, 0, 255).astype(np.uint8)
        g = np.clip(g, 0, 255).astype(np.uint8)
        b = np.clip(b, 0, 255).astype(np.uint8)
        
        # RGB 채널을 하나의 배열로 결합 (H, W, 3)
        rgb = np.stack((r, g, b), axis=2)
        
        # OutputImageRawFrame 생성
        converted_frame = OutputImageRawFrame(
            image=rgb.tobytes(),
            size=(video_frame.width, video_frame.height),
            format="RGB",
        )
        converted_frame.pts = video_frame.pts
        await self.push_frame(converted_frame)


SimliVideoService._consume_and_process_video = _consume_and_process_video

# WebRTC를 위한 STUN 서버
STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
]

# 활성 WebRTC 연결 저장소
pcs: Dict[str, Tuple[SmallWebRTCConnection, SmallWebRTCTransport, Optional[PipelineTask]]] = {}


# FastAPI 수명 주기 관리
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 애플리케이션 시작
    yield  # 애플리케이션 실행
    
    # 애플리케이션 종료 정리
    active_connections = list(pcs.values())  # 반복 중 수정 방지를 위한 복사
    
    # 모든 활성 WebRTC 연결 종료 시도
    cleanup_tasks = []
    for conn, _, _ in active_connections:
        cleanup_tasks.append(conn.disconnect())

    # 모든 연결 종료 태스크 병렬 실행
    results = await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    
    # 전역 연결 맵 정리
    pcs.clear()


# FastAPI 앱 생성
app = FastAPI(lifespan=lifespan)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 정적 파일 제공
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """루트 경로 - 데모 페이지로 리다이렉트"""
    return RedirectResponse(url="/static/index.html")


@app.post("/")
async def root_post(request: Request, background_tasks: BackgroundTasks):
    """루트 POST - 핸드셰이크 또는 오퍼 처리"""
    body = await request.json()
    if "rtvi_client_version" in body:
        return connect_logic(body)
    if "sdp" in body and "type" in body:
        return await offer_logic(body, background_tasks)
    raise HTTPException(status_code=400, detail="invalid body")


@app.post("/offer")
async def offer_handler(request: Request, background_tasks: BackgroundTasks):
    """명시적 오퍼 엔드포인트"""
    body = await request.json()
    if "rtvi_client_version" in body:
        return connect_logic(body)
    return await offer_logic(body, background_tasks)


@app.post("/ice")
async def ice_handler(request: Request):
    """ICE 후보 교환 엔드포인트"""
    body = await request.json()
    await ice_logic(body)
    return Response(status_code=204)


@app.get("/status")
async def status_handler():
    """서버 상태 확인 엔드포인트"""
    status_info = {
        "server_version": "0.0.63_refactored",
        "active_connections": len(pcs),
        "connections": []
    }

    for pc_id, (conn, _, task) in pcs.items():
        task_running = task is not None and not task.done()
        conn_info = {
            "id": pc_id,
            "ice_state": getattr(conn, "ice_connection_state", "unknown"),
            "connection_state": getattr(conn, "connection_state", "unknown"),
            "pipeline_running": task_running
        }
        status_info["connections"].append(conn_info)

    return status_info


def connect_logic(body: dict) -> dict:
    """클라이언트 연결 핸드셰이크 처리"""
    client_id = body.get("client_id") or str(uuid.uuid4())
    ver = body.get("rtvi_client_version")
    return {"client_id": client_id, "iceServers": [{"urls": u} for u in STUN_SERVERS]}


async def offer_logic(body: dict, background_tasks: BackgroundTasks) -> dict:
    """SDP 오퍼 처리 및 WebRTC 연결 설정"""
    sdp = body.get("sdp")
    typ = body.get("type")
    # pc_id를 기본으로 사용
    pc_id = body.get("pc_id") or body.get("client_id")
    restart_pc = body.get("restart_pc", False)

    if not (sdp and typ):
        raise HTTPException(status_code=400, detail="missing 'sdp' or 'type'")

    if pc_id and pc_id in pcs and not restart_pc:
        # 기존 연결로 재협상
        conn, _, _ = pcs[pc_id]
        await conn.renegotiate(sdp=sdp, type=typ)
    else:
        # 필요한 경우 이전 연결 정리
        if pc_id and pc_id in pcs:
            existing_conn, _, _ = pcs[pc_id]
            await existing_conn.disconnect()
            pcs.pop(pc_id, None)

        # 새 연결 생성
        conn = SmallWebRTCConnection(ice_servers=STUN_SERVERS)
        
        transport = SmallWebRTCTransport(
            webrtc_connection=conn,
            params=TransportParams(
                audio_in_enabled=True,
                audio_in_filter=NoisereduceFilter(),
                audio_out_enabled=True,
                video_out_enabled=True,
                vad_analyzer=CustomVADAnalyzer(params=VADParams(stop_secs=0.4)),
            ),
        )

        # Connection 'closed' 핸들러: 전역 맵에서 제거하는 역할만 수행
        @conn.event_handler("closed")
        async def handle_connection_closed(connection: SmallWebRTCConnection):
            pcs.pop(connection.pc_id, None)

        background_tasks.add_task(run_bot, conn, transport, pcs)
        await conn.initialize(sdp=sdp, type=typ)
        pcs[conn.pc_id] = (conn, transport, None)

    answer = conn.get_answer()
    pcs[answer["pc_id"]] = pcs.pop(conn.pc_id) if conn.pc_id in pcs else (conn, transport, None)

    return answer


async def ice_logic(body: dict):
    """ICE 후보 처리"""
    pc_id = body.get("pc_id") or body.get("client_id")
    candidate = body.get("candidate")
    
    if not (pc_id and candidate):
        raise HTTPException(status_code=400, detail="missing 'pc_id' or 'candidate'")
        
    if pc_id not in pcs:
        # ICE 후보는 초기 연결 단계에서 빠르게 교환될 수 있으므로
        # 오류 대신 경고만 표시하고 진행
        return
        
    conn, _, _ = pcs[pc_id]
    await conn.add_ice_candidate(candidate)


def start_server():
    """서버 시작 함수"""
    load_dotenv(override=True)

    import argparse
    parser = argparse.ArgumentParser(description="WebRTC 서버")
    parser.add_argument("--host", default="0.0.0.0", help="서버 호스트 (기본값: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="서버 포트 (기본값: 8080)")
    parser.add_argument("--debug", action="store_true", help="디버그 모드 활성화 (상세 로깅)")
    parser.add_argument("--stt", default="whisper", choices=["deepgram", "whisper"], help="STT 서비스 선택 (기본값: whisper)")
    args = parser.parse_args()

    print(f"서버 시작: http://{args.host}:{args.port}")
    print("Ctrl+C로 서버를 정상 종료할 수 있습니다.")
    print(f"STT 서비스: {os.getenv('STT_SERVICE')}")
    
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", lifespan="on")


if __name__ == "__main__":
    start_server()