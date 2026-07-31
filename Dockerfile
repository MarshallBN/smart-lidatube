FROM python:3.12-alpine

# Set build arguments
ARG RELEASE_VERSION
ENV RELEASE_VERSION=${RELEASE_VERSION}

# Install ffmpeg and su-exec
RUN apk update && apk add --no-cache ffmpeg chromaprint su-exec deno curl

# Create directories and set permissions
COPY . /lidatube
WORKDIR /lidatube

# Install requirements
ENV PYTHONPATH=/lidatube/src
RUN pip install --no-cache-dir -r requirements.txt

# Make the script executable
RUN chmod +x thewicklowwolf-init.sh

# Expose port
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:5000/health || exit 1

# Start the app
ENTRYPOINT ["./thewicklowwolf-init.sh"]

