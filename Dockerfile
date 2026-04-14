FROM python:3.14-slim

WORKDIR /home/primary-user

COPY main.py main.py
COPY requirements.txt requirements.txt

# Install the modules.
RUN pip3 install --no-cache-dir -r requirements.txt

# Project files will exist here.
RUN mkdir -p /home/project
RUN adduser primary-user && \
    adduser primary-user primary-user && \
    adduser primary-user root && \
    chown -R primary-user /home/primary-user /home/project

ENV HOME /home/primary-user
USER primary-user

EXPOSE 3000
CMD [ "python", "./main.py" ]
