FROM python:3.12

## set environment variables
#ENV PYTHONDONTWRITEBYTECODE 1
#ENV PYTHONUNBUFFERED 1

RUN apt-get update && apt-get -y install nginx

# Note: /etc/nginx/sites-available/default is rendered by init.sh at container
# start from the /code/.nginx/default_80.conf template (listen port = $PORT,
# default 80), so PaaS platforms can assign the port at runtime.

# Set the working directory in the container
WORKDIR /code

# Copy the current directory contents into the container at /app
COPY . /code

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The web entrypoint starts nginx as root, then drops gunicorn to this user.
# Celery worker and beat services run as this user from the start.
RUN groupadd --system cloudmoo && \
    useradd --system --gid cloudmoo --home-dir /code --shell /usr/sbin/nologin --no-create-home cloudmoo

# Make port 80 available to the world outside this container
EXPOSE 80

COPY init.sh /usr/local/bin/
RUN chmod u+x /usr/local/bin/init.sh

#COPY init.sh init.sh

ENTRYPOINT ["init.sh"]
