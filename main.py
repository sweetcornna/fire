# 尝试从 .env 文件加载环境变量
import os
if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

def main():
    from utils.config import get_config

    config = get_config()
    if config.get("previewOnly", False):
        from datetime import date

        from core.forms import build_ai_message

        count = config.get("previewCount", 5)
        successful = 0
        print(f"AI 消息预览（共 {count} 条，均未发送）：")
        for index in range(1, count + 1):
            try:
                print(f"{index}. {build_ai_message(date.today(), config)}")
                successful += 1
            except Exception as exc:
                print(f"{index}. [生成失败] {exc}")
        if successful == 0:
            raise SystemExit("全部 AI 预览均生成失败")
        return

    from core.tasks import runTasks

    runTasks()


if __name__ == "__main__":
    main()
