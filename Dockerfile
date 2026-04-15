FROM python:3.14-slim

WORKDIR /home/primary

COPY main.py main.py
COPY requirements.txt requirements.txt

# Install the modules.
RUN pip3 install --no-cache-dir -r requirements.txt

# Project files will exist here.
RUN mkdir -p /home/project
RUN adduser primary && \
    adduser primary primary && \
    adduser primary root && \
    chown -R primary /home/primary && \
    chown -R primary /home/project;

ENV HOME /home/primary
USER primary

EXPOSE 3000
CMD [ "python", "./main.py" ]
