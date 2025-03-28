# backend/app/websocket/endpoints/camera.py

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.database.qdrant import QdrantDatabase
from app.services.AIService import AIService
from app.services.ClickPictureService import ClickPictureService
from app.services.OCRService import OCRService
from app.services.WebSocketService import WebSocketService
from app.utils.logger import CustomLogger

router = APIRouter()
click_picture_service = ClickPictureService(settings.GCP_BUCKET_NAME)
ocr_service = OCRService()
ai_service = AIService()
qdrant_db = QdrantDatabase()


@router.websocket("/click-picture")
async def camera_websocket_endpoint(websocket: WebSocket):
    camera_id = None

    try:
        camera_id = await WebSocketService.connect_camera(websocket)
        if not camera_id:
            return

        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("action") == "start":
                    if not click_picture_service.initialize_camera():
                        await websocket.close(
                            code=1011, reason="Failed to initialize camera"
                        )
                        return

                    qdrant_db.delete_all_documents()
                    click_picture_service.delete_captures()

                    response = {"action": "start"}
                    await websocket.send_text(json.dumps(response))
                    await WebSocketService.broadcast_to_frontend(response)

                elif message.get("action") == "click":
                    result = click_picture_service.capture_picture()
                    if not result["success"]:
                        response = {
                            "status": "error",
                            "message": f"Failed to capture picture: {result['error']}",
                        }
                        await websocket.send_text(json.dumps(response))
                        await WebSocketService.broadcast_to_frontend(response)
                        continue

                    try:
                        CustomLogger.create_log(
                            "info", f"Captured picture: {result['file_path']}"
                        )

                        page_number = str(
                            result["file_path"]
                            .split("/")[-1]
                            .split(".")[0]
                            .split("_")[1]
                        )
                        scanning_response = {
                            "action": "scanning",
                            "page_number": page_number,
                        }
                        await WebSocketService.broadcast_to_frontend(scanning_response)

                        ocr_text = ocr_service.ocr_image(result["file_path"])
                        CustomLogger.create_log("info", f"OCR Text: {ocr_text}")

                        cleaned_text = await ai_service.cleanup_ocr_text(ocr_text)
                        CustomLogger.create_log("info", f"Cleaned Text: {cleaned_text}")
                        qdrant_db.add_document(
                            cleaned_text, page_number, result["file_path"]
                        )

                        processing_response = {
                            "action": "processing",
                            "page_number": page_number,
                        }
                        await WebSocketService.broadcast_to_frontend(
                            processing_response
                        )

                        response = {
                            "action": "click",
                            "page_number": page_number,
                            "ocr_text": ocr_text,
                        }
                    except Exception as e:
                        response = {
                            "status": "error",
                            "message": f"Failed to process image: {str(e)}",
                            "file_path": result["file_path"],
                        }

                    await websocket.send_text(json.dumps(response))
                    await WebSocketService.broadcast_to_frontend(response)

                elif message.get("action") == "end":
                    response = {"action": "end"}
                    await WebSocketService.broadcast_to_frontend(response)

                    click_picture_service.release_camera()

                    await asyncio.sleep(2)
                    await websocket.close()
                    return
                else:
                    response = {"status": "error", "message": "Invalid action"}
                    await websocket.send_text(json.dumps(response))
                    await WebSocketService.broadcast_to_frontend(response)
            except WebSocketDisconnect:
                raise
            except Exception as e:
                CustomLogger.create_log("error", f"Error processing message: {str(e)}")
                response = {"status": "error", "message": str(e)}
                await websocket.send_text(json.dumps(response))

    except WebSocketDisconnect:
        click_picture_service.release_camera()
    except Exception as e:
        CustomLogger.create_log("error", f"Error in websocket connection: {str(e)}")
        click_picture_service.release_camera()
    finally:
        if camera_id:
            WebSocketService.disconnect_camera(camera_id)
        try:
            await websocket.close()
        except:
            pass
