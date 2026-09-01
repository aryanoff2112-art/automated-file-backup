import os
import shutil
import datetime
import schedule
import time

source_dir = "/path/to/source"
destination_dir = "/path/to/destination"

def copy_folder_to_directory(source, destination):
    today = datetime.date.today()
    dest_dir = os.path.join(destination, str(today))

    try:
        shutil.copytree(source, dest_dir)
        print(f"Folder copied successfully to {dest_dir}")

    except FileExistsError:
        print(f"Folder already exists in : {destination}")    

schedule.every().day.at("00:00").do(lambda: copy_folder_to_directory(source_dir, destination_dir))

while True:
    schedule.run.pending()
    time.sleep(60)
