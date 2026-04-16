FROM python:3.14-slim

USER root
RUN apt-get update && apt-get install -y coreutils sudo && \
    echo "primary ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/primary

# External project files will exist here.
RUN mkdir -p /home/project && \
    useradd -m -s /bin/bash -G root primary && \
    chown -R primary:primary /home/primary && \
    chown -R primary:primary /home/project;

# Ensure that HOME is the primary user's dir.
ENV HOME="/home/primary"
# Ensures Python bins can be run
ENV PATH="$PATH:/home/primary/.local/bin"

# Work as the user.
USER primary

# Ensure code files are stored in the user's home directory.
WORKDIR /home/primary

COPY main.py main.py
COPY requirements.txt requirements.txt

# Install the modules.
RUN pip3 install --no-cache-dir -r requirements.txt && sudo chown -R primary:primary /home/primary

EXPOSE 3000

CMD [ "python3", "./main.py" ]
