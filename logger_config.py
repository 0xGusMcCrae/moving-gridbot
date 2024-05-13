import logging
from datetime import datetime
import os

class LoggerConfig:
    def __init__(self, name='gridbot', log_dir='logs'):
        """
        Initialize the logger with a unique name and file for each session.

        Args:
            name (str): The base name for the logger and log file.
            log_dir (str): The directory where log files are saved.
        """
        # Ensure the log directory exists
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # Create a new logger with the provided name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)

        # Prevent logging duplication by disabling propagation
        self.logger.propagate = False

        # Create a file handler
        file_handler = logging.FileHandler(f"logs/{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        file_handler.setLevel(logging.INFO)

        # Create a stream handler (console)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        # Define a common formatter
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # Add handlers to the logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def get_logger(self):
        """
        Get the configured logger instance.
        
        Returns:
            Logger: The configured logger instance.
        """
        return self.logger
