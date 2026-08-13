# Railway deployment

CloudMoo runs as five Railway services: a public `web` service, `worker`,
single-instance `beat`, PostgreSQL, and RabbitMQ. Railway does not run the
repository's Docker Compose file directly, so the three application services
must be created from the same repository.

## Create the stack

1. Create a Railway project and add a PostgreSQL service named `Postgres`.
2. Add a private Docker-image service named `RabbitMQ` using
   `rabbitmq:3-management`. Set these variables on that service:

   ```text
   RABBITMQ_DEFAULT_USER=cloudmoo
   RABBITMQ_DEFAULT_PASS=${{secret(64)}}
   RABBITMQ_DEFAULT_VHOST=/
   ```

3. Add the CloudMoo GitHub repository three times and name the services
   `web`, `worker`, and `beat`. Use the matching Config as Code file in each
   service's Settings → Deploy configuration:

   | Service | Config file | Public networking |
   | --- | --- | --- |
   | `web` | `/deploy/railway/web.railway.json` | Generate a public domain |
   | `worker` | `/deploy/railway/worker.railway.json` | Private only |
   | `beat` | `/deploy/railway/beat.railway.json` | Private only |

4. In each CloudMoo service, use the variables in
   [`variables.example.env`](variables.example.env). Railway resolves the
   `Postgres`, `RabbitMQ`, and `web` references inside one project and
   environment. The web service's generated domain is used for host and CSRF
   settings.
5. Configure SMTP variables before inviting real users. The production
   default is Django SMTP; without SMTP settings, verification messages are
   not delivered.

The `beat` service must remain at one replica. Scaling it above one duplicates
scheduled monitoring and cleanup tasks.

## Publish a one-click Railway button

After creating the stack once, use Railway's **Generate Template from Project**
action and copy the generated template URL into the README button format:

```markdown
[![Deploy on Railway](https://railway.com/button.svg)](YOUR_RAILWAY_TEMPLATE_URL)
```

The template URL is created in the Railway account/workspace rather than in
the Git repository, so it must be generated and published by the CloudMoo
workspace owner. The repository files above are the source-controlled recipe
that keeps that template reproducible.
