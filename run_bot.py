from loguru import logger
from tts.tts_service import TTSPipecService
from simli import SimliConfig
from pipecat.services.simli.video import SimliVideoService
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transcriptions.language import Language
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from utils.get_weather import get_weather
from pipecat.services.google.llm import GoogleLLMService
from stt.whisper_stt_service import WhisperSTTService
from debug_tools.logging_processor import LoggingProcessor
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection
from pipecat.processors.frameworks.rtvi import RTVIConfig, RTVIObserver, RTVIProcessor
from pipecat.services.deepgram.tts import DeepgramTTSService
from deepgram import LiveOptions
from typing import Optional
import asyncio
import logging
import os



async def run_bot(connection: SmallWebRTCConnection, transport: SmallWebRTCTransport, pcs):
	pc_id = connection.pc_id

	pipeline_task: Optional[PipelineTask] = None # 나중에 할당

	try:
		logger_proc = LoggingProcessor()
		

		llm_model = "gemini-2.0-flash-lite"
		llm_system_prompt = "You are a fast, low-latency chatbot. Respond to what the user said in a creative and helpful way, but keep responses short and legible. Ensure responses contain only words. Check again that you have not included special characters other than '?' or '!'."
		tts_speed = 1.2

		llm = GoogleLLMService(
			api_key=os.getenv("GEMINI_API_KEY"),
			model=llm_model,
			params=GoogleLLMService.InputParams(temperature=1, language=Language.KO_KR, thinking_budget=0),
			system_prompt=llm_system_prompt
		)
		
		rtvi = RTVIProcessor(config=RTVIConfig(config=[]), transport=transport)
		simli = SimliVideoService(
			SimliConfig(
				apiKey=os.getenv("SIMLI_API_KEY"),
				faceId=os.getenv("SIMLI_FACE_ID"),
				syncAudio=True,
				handleSilence=True,
				maxSessionLength=3000,
				maxIdleTime=30
			),
			latency_interval=0
		)

		# tts = TTSPipecService(
		# 	voice="KR",
		# 	speed=tts_speed,
		# 	Language=Language.KO,
		# )

		tts = DeepgramTTSService(
			api_key=os.getenv("DEEPGRAM_API_KEY"),
			voice="aura-helios-en",
			sample_rate=24000
		)
			
		
		weather_function = FunctionSchema(
			name="get_current_weather",
			description="Get the current weather for a specific location",
			properties={
				"location": {
					"type": "string",
					"description": "The city and state or city name, e.g. Seoul or 서울"
				},
				"format": {
					"type": "string",
					"enum": ["celsius", "fahrenheit"],
					"description": "The temperature unit to use"
				}
			},
			required=["location"]
		)
		async def fetch_weather(function_name, tool_call_id, args, llm, context, result_callback):
			location = args.get("location", "서울")
			format = args.get("format", "celsius")
			
			weather_data = get_weather(location, format)
			
			await result_callback(weather_data)


		system_prompt = """특수문자를 사용하면 안됩니다.[절대사용하지말아야할 문자 : *, / ]저는 한국인을 위한 영어 표현 코치 AI 에이전트입니다. 한국어 질문에 영어 표현과 간단한 한국어 설명을 제공합니다. 사용자의 한국어 입력을 이해하고 적절한 영어 표현을 자연스러운 발음으로 들려줍니다.
날씨, 영어 표현, 간단한 회화 등 일상 질문에 답변하며, 특히 비즈니스, 여행, 일상 영어 표현에 특화되어 있습니다. STT로 인한 오타나 인식 오류가 있더라도 문맥을 고려해 의도를 파악합니다.
한국어 또는 영어로만 답변하며, 깔끔하고 자연스러운 문장으로 TTS에 최적화된 응답을 제공합니다 구체적인 씬 예시에 맞는 질문을 받는다면 구체적인 씬 예시의 답변을 제공합니다.
시나리오 정보:
- 비즈니스 영어: 회의, 이메일, 프레젠테이션 표현
- 여행 영어: 호텔, 레스토랑, 교통, 쇼핑 관련 표현
- 일상 영어: 인사, 소개, 취미, 날씨 대화

구체적인 씬 예시:
처음 인사말 :
"안녕하세요! 영어 표현 코치 AI입니다. 어떤 영어 표현이 필요하신가요?"
[비즈니스 영어 씬]
사용자: 외국 동료에게 프로젝트 지연을 알리는 이메일을 어떻게 쓰면 좋을까요?
AI 코치: 프로젝트 지연 안내 이메일은 다음과 같이 작성할 수 있습니다:
"I regret to inform you that there will be a delay in our project timeline due to technical issues. The new expected completion date is May 25th."

[여행 영어 씬]
사용자: 택시 기사에게 호텔로 데려다 달라고 하려면 뭐라고 해야 하나요?
AI 코치: "Could you take me to Hotel Metropole, please? It's on Rue de Lyon."

[일상 영어 씬 - 인터럽트 기능 포함]
사용자: 날씨에 대해 대화할 때 어떤 표현을 쓸 수 있나요?
AI 코치: "The weather is really nice today, isn't it?"
동료: "Yes, it's beautiful! Perfect blue skies."
AI 코치: 이에 대해 이렇게 이어갈 수 있어요: "I heard it's supposed to stay this way all week. I'm thinking about having a picnic this weekend."
사용자: 비가 올 때는요?
AI 코치: "This rain is really coming down heavily, isn't it? I forgot my umbrella today."
"""
		tools = ToolsSchema(standard_tools=[weather_function])
		context = OpenAILLMContext(
			messages=[
				{
					"role": "system",
					"content": system_prompt,
				}
			],
			tools=tools,
		)
		llm.register_function("get_current_weather", fetch_weather)
		agg = llm.create_context_aggregator(context=context)

		whisper_model = "base"
		stt = WhisperSTTService(
			model_name=whisper_model,
		)
		pipeline = Pipeline([
			transport.input(),
			rtvi,
			stt,
			agg.user(),
			llm,
			tts,
			simli,
			transport.output(),
			agg.assistant()
		])

		pipeline_task = PipelineTask(
			pipeline,
			params=PipelineParams(
				allow_interruptions=True,
				enable_metrics=True,
				report_only_initial_ttfb=True,
			),
			observers=[RTVIObserver(rtvi)],
		)

		if pc_id in pcs:
			conn, tr, _ = pcs[pc_id]
			pcs[pc_id] = (conn, tr, pipeline_task)
			logger.info(f"[run_bot:{pc_id}] Pipeline Task 생성 및 pcs 업데이트 완료")
		else:
			logger.error(f"[run_bot:{pc_id}] 시작 시점에 pcs 맵에 ID가 없음!")
			return

		@transport.event_handler("on_client_connected")
		async def on_client_connected(tr, client):
			logger.info(f"[run_bot:{pc_id}] 🔗 Transport: 클라이언트 연결됨")
			await pipeline_task.queue_frames([agg.user().get_context_frame()])
			await asyncio.sleep(2)
			logger.info(f"[run_bot:{pc_id}] 🤖 BotReady 메시지 전송 시도")
			await rtvi.set_bot_ready()
			logger.info(f"[run_bot:{pc_id}] ✅ BotReady 메시지 전송 완료")

		@transport.event_handler("on_client_disconnected")
		async def on_client_disconnected(tr, client):
			logger.warning(f"[run_bot:{pc_id}] ⚠️ Transport: 클라이언트 연결 끊김 (일시적일 수 있음)")

		@transport.event_handler("on_client_closed")
		async def on_client_closed(tr, client):
			logger.info(f"[run_bot:{pc_id}] 🔌 Transport: 클라이언트 닫힘 감지됨. 파이프라인 태스크 취소 시도.")
			if pipeline_task:
				await pipeline_task.cancel()
				logger.info(f"[run_bot:{pc_id}] 파이프라인 태스크 취소 요청 완료.")
			else:
				logger.warning(f"[run_bot:{pc_id}] on_client_closed 호출 시점에 pipeline_task가 없음!")

		runner = PipelineRunner(force_gc=True, handle_sigint=False)

		logger.info("파이프라인 실행 시작...")
		await runner.run(pipeline_task)
	except asyncio.CancelledError:
		logger.info(f"실행 중단: {pc_id}")
	except Exception as e:
		logger.error(f"[run_bot:{pc_id}] 오류 발생: {e}")
		if pipeline_task:
			await pipeline_task.cancel()
	finally:
		logger.info(f"파이프라인 종료")