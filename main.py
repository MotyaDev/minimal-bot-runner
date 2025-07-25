import subprocess
import sys
import time
import logging
import signal
import os
import threading
import psutil
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor
import re

class MinimalBotRunner:
    def __init__(self, bot_files: List[str]):
        """Initialize minimal bot runner with just file list"""
        self.bot_files = bot_files
        self.current_processes = {}
        self.shutdown_requested = False
        self.file_loggers = {}
        self.restart_counts = {}
        self.executor = ThreadPoolExecutor(max_workers=len(bot_files))
        
        # Fixed settings (no configuration needed)
        self.MAX_RESTARTS = 5
        self.RESTART_DELAY = 10
        self.MEMORY_LIMIT_MB = 400
        
        self.setup_logging()
        
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        self.logger.info(f"🤖 Minimal Bot Runner started for {len(bot_files)} files")

    def setup_logging(self):
        """Simple logging setup"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler()]
        )
        self.logger = logging.getLogger("MinimalRunner")

    def create_file_logger(self, file_path: Path) -> logging.Logger:
        """Create simple logger for file"""
        file_name = file_path.stem
        logger_name = f"Bot_{file_name}"
        
        if logger_name in self.file_loggers:
            return self.file_loggers[logger_name]
        
        file_logger = logging.getLogger(logger_name)
        file_logger.setLevel(logging.INFO)
        
        formatter = logging.Formatter(f'%(asctime)s - [{file_name}] %(message)s')
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        file_logger.addHandler(console_handler)
        file_logger.propagate = False
        
        self.file_loggers[logger_name] = file_logger
        return file_logger

    def is_info_message(self, message: str) -> bool:
        """Check if stderr message is actually just info"""
        info_keywords = ['info', 'debug', 'successfully', 'started', 'initialized', 'connected', 'polling', 'running']
        return any(keyword in message.lower() for keyword in info_keywords)

    def signal_handler(self, signum, frame):
        """Handle shutdown"""
        self.logger.info(f"🛑 Shutting down...")
        self.shutdown_requested = True
        self.stop_all_processes()

    def start_process(self, file_path: Path) -> Optional[subprocess.Popen]:
        """Start bot process"""
        if not file_path.exists():
            self.logger.error(f"❌ File {file_path} not found!")
            return None
        
        file_logger = self.create_file_logger(file_path)
        
        try:
            file_logger.info("🚀 Starting...")
            
            process = subprocess.Popen(
                [sys.executable, str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                cwd=file_path.parent
            )
            
            file_logger.info(f"✅ Started with PID: {process.pid}")
            return process
            
        except Exception as e:
            file_logger.error(f"❌ Start failed: {e}")
            return None

    def monitor_output(self, file_path: Path, process: subprocess.Popen):
        """Monitor process output"""
        file_logger = self.create_file_logger(file_path)
        
        def read_stdout():
            while process.poll() is None and not self.shutdown_requested:
                try:
                    line = process.stdout.readline()
                    if line:
                        file_logger.info(line.strip())
                except:
                    break
                    
        def read_stderr():
            while process.poll() is None and not self.shutdown_requested:
                try:
                    line = process.stderr.readline()
                    if line:
                        clean_line = line.strip()
                        if self.is_info_message(clean_line):
                            file_logger.info(clean_line)
                        else:
                            file_logger.error(f"🚨 {clean_line}")
                except:
                    break
        
        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

    def monitor_memory(self, file_path: Path, process: subprocess.Popen):
        """Simple memory monitoring"""
        file_logger = self.create_file_logger(file_path)
        
        def memory_check():
            try:
                psutil_process = psutil.Process(process.pid)
                while process.poll() is None and not self.shutdown_requested:
                    try:
                        memory_mb = psutil_process.memory_info().rss / 1024 / 1024
                        if memory_mb > self.MEMORY_LIMIT_MB:
                            file_logger.warning(f"⚠️ High memory: {memory_mb:.1f}MB, restarting...")
                            process.terminate()
                            break
                        time.sleep(30)
                    except:
                        break
            except:
                pass
        
        threading.Thread(target=memory_check, daemon=True).start()

    def run_single_bot(self, file_path: Path):
        """Run single bot with auto-restart"""
        file_logger = self.create_file_logger(file_path)
        restart_count = 0
        
        while not self.shutdown_requested and restart_count < self.MAX_RESTARTS:
            start_time = time.time()
            
            process = self.start_process(file_path)
            if not process:
                break
            
            self.current_processes[str(file_path)] = process
            
            # Start monitoring
            self.monitor_output(file_path, process)
            self.monitor_memory(file_path, process)
            
            # Wait for completion
            exit_code = process.wait()
            runtime = time.time() - start_time
            
            file_logger.info(f"📊 Ended: code={exit_code}, time={runtime:.1f}s")
            
            # Check if restart needed
            if exit_code == 0:
                file_logger.info("✅ Completed successfully")
                break
            elif exit_code in [130, 143, -15]:  # Manual termination
                file_logger.info("🔄 Manual stop")
                break
            else:
                restart_count += 1
                if restart_count < self.MAX_RESTARTS:
                    delay = self.RESTART_DELAY * restart_count
                    file_logger.warning(f"🔄 Restart #{restart_count} in {delay}s...")
                    
                    for _ in range(delay):
                        if self.shutdown_requested:
                            return
                        time.sleep(1)
                else:
                    file_logger.error("🛑 Max restarts reached")

    def stop_process(self, file_path: Path):
        """Stop specific process"""
        process = self.current_processes.get(str(file_path))
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except:
                try:
                    process.kill()
                    process.wait()
                except:
                    pass
            finally:
                if str(file_path) in self.current_processes:
                    del self.current_processes[str(file_path)]

    def stop_all_processes(self):
        """Stop all processes"""
        for file_path in list(self.current_processes.keys()):
            self.stop_process(Path(file_path))

    def run(self):
        """Main run method"""
        self.logger.info("🚀 Starting all bots...")
        
        # Validate files
        valid_files = []
        for file_str in self.bot_files:
            file_path = Path(file_str)
            if file_path.exists():
                valid_files.append(file_path)
                self.logger.info(f"✅ {file_path}")
            else:
                self.logger.error(f"❌ {file_path} not found")
        
        if not valid_files:
            self.logger.error("❌ No valid files!")
            return
        
        try:
            # Run all files in parallel
            futures = []
            for file_path in valid_files:
                future = self.executor.submit(self.run_single_bot, file_path)
                futures.append(future)
            
            # Wait for all
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    self.logger.error(f"❌ Error: {e}")
                    
        except Exception as e:
            self.logger.error(f"💥 Critical error: {e}")
        finally:
            self.stop_all_processes()
            self.executor.shutdown(wait=True)


def main():
    """Ultra simple main function"""
    # ТОЛЬКО список файлов - ВСЁ!
    bot_files = [
        'enze.py',
        # Добавьте другие боты здесь
    ]
    
    try:
        runner = MinimalBotRunner(bot_files)
        runner.run()
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
    except Exception as e:
        print(f"💥 Error: {e}")
    finally:
        print("🤖 Done")


if __name__ == "__main__":
    main()
