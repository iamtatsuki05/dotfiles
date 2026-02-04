# デプロイパターン集

## 目次

1. [コンテナデプロイ](#コンテナデプロイ)
2. [クラウドプロバイダー別](#クラウドプロバイダー別)
3. [Kubernetes](#kubernetes)
4. [サーバーレス](#サーバーレス)
5. [静的サイト](#静的サイト)
6. [デプロイ戦略](#デプロイ戦略)

## コンテナデプロイ

### マルチプラットフォームビルド

```yaml
# GitHub Actions
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-qemu-action@v3

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: docker/build-push-action@v5
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

### Amazon ECR

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/github-actions
          aws-region: ap-northeast-1

      - uses: aws-actions/amazon-ecr-login@v2
        id: login-ecr

      - uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.login-ecr.outputs.registry }}/app:${{ github.sha }}
```

### Google Artifact Registry

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/123456/locations/global/workloadIdentityPools/github/providers/github
          service_account: github-actions@project.iam.gserviceaccount.com

      - uses: google-github-actions/setup-gcloud@v2

      - run: gcloud auth configure-docker asia-northeast1-docker.pkg.dev

      - uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: asia-northeast1-docker.pkg.dev/project/repo/app:${{ github.sha }}
```

## クラウドプロバイダー別

### AWS ECS

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/deploy-role
          aws-region: ap-northeast-1

      - name: Download task definition
        run: |
          aws ecs describe-task-definition \
            --task-definition app \
            --query taskDefinition > task-definition.json

      - name: Update task definition
        id: task-def
        uses: aws-actions/amazon-ecs-render-task-definition@v1
        with:
          task-definition: task-definition.json
          container-name: app
          image: ${{ env.ECR_REGISTRY }}/app:${{ github.sha }}

      - name: Deploy to ECS
        uses: aws-actions/amazon-ecs-deploy-task-definition@v1
        with:
          task-definition: ${{ steps.task-def.outputs.task-definition }}
          service: app-service
          cluster: production
          wait-for-service-stability: true
```

### AWS Lambda

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/deploy-role
          aws-region: ap-northeast-1

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: npm ci && npm run build

      - run: zip -r function.zip dist node_modules

      - name: Deploy Lambda
        run: |
          aws lambda update-function-code \
            --function-name my-function \
            --zip-file fileb://function.zip
```

### Google Cloud Run

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/123456/locations/global/workloadIdentityPools/github/providers/github
          service_account: deploy@project.iam.gserviceaccount.com

      - uses: google-github-actions/deploy-cloudrun@v2
        with:
          service: app
          region: asia-northeast1
          image: asia-northeast1-docker.pkg.dev/project/repo/app:${{ github.sha }}
          env_vars: |
            NODE_ENV=production
```

### Azure Container Apps

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: azure/login@v1
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - uses: azure/container-apps-deploy-action@v1
        with:
          resourceGroup: rg-production
          containerAppName: app
          imageToDeploy: ghcr.io/${{ github.repository }}:${{ github.sha }}
```

## Kubernetes

### kubectl

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: azure/setup-kubectl@v3

      - name: Set kubeconfig
        run: |
          mkdir -p ~/.kube
          echo "${{ secrets.KUBECONFIG }}" | base64 -d > ~/.kube/config

      - name: Deploy
        run: |
          kubectl set image deployment/app \
            app=ghcr.io/${{ github.repository }}:${{ github.sha }} \
            -n production

          kubectl rollout status deployment/app -n production --timeout=5m
```

### Helm

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: azure/setup-helm@v3

      - name: Deploy with Helm
        run: |
          helm upgrade --install app ./charts/app \
            --namespace production \
            --set image.tag=${{ github.sha }} \
            --set image.repository=ghcr.io/${{ github.repository }} \
            --values ./charts/app/values-production.yaml \
            --wait --timeout 5m
```

### Kustomize

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Update image tag
        run: |
          cd k8s/overlays/production
          kustomize edit set image app=ghcr.io/${{ github.repository }}:${{ github.sha }}

      - name: Deploy
        run: |
          kustomize build k8s/overlays/production | kubectl apply -f -
          kubectl rollout status deployment/app -n production
```

### ArgoCD

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: org/gitops-repo
          token: ${{ secrets.GITOPS_TOKEN }}

      - name: Update image tag
        run: |
          cd apps/app/overlays/production
          kustomize edit set image app=ghcr.io/${{ github.repository }}:${{ github.sha }}

      - name: Commit and push
        run: |
          git config user.name "GitHub Actions"
          git config user.email "actions@github.com"
          git add .
          git commit -m "Deploy app: ${{ github.sha }}"
          git push
```

## サーバーレス

### Vercel

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: amondnet/vercel-action@v25
        with:
          vercel-token: ${{ secrets.VERCEL_TOKEN }}
          vercel-org-id: ${{ secrets.VERCEL_ORG_ID }}
          vercel-project-id: ${{ secrets.VERCEL_PROJECT_ID }}
          vercel-args: '--prod'
```

### Netlify

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: npm ci && npm run build

      - uses: nwtgck/actions-netlify@v3
        with:
          publish-dir: './dist'
          production-deploy: true
        env:
          NETLIFY_AUTH_TOKEN: ${{ secrets.NETLIFY_AUTH_TOKEN }}
          NETLIFY_SITE_ID: ${{ secrets.NETLIFY_SITE_ID }}
```

### Cloudflare Workers

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: npm ci

      - name: Deploy
        run: npx wrangler deploy
        env:
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
```

## 静的サイト

### GitHub Pages

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: npm ci && npm run build

      - uses: actions/configure-pages@v4

      - uses: actions/upload-pages-artifact@v3
        with:
          path: './dist'

      - id: deployment
        uses: actions/deploy-pages@v4
```

### S3 + CloudFront

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: '20'

      - run: npm ci && npm run build

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/deploy-role
          aws-region: ap-northeast-1

      - name: Deploy to S3
        run: aws s3 sync dist/ s3://bucket-name --delete

      - name: Invalidate CloudFront
        run: |
          aws cloudfront create-invalidation \
            --distribution-id ${{ secrets.CLOUDFRONT_DISTRIBUTION_ID }} \
            --paths "/*"
```

## デプロイ戦略

### Blue-Green デプロイ

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to green
        run: |
          kubectl apply -f k8s/green/
          kubectl rollout status deployment/app-green -n production

      - name: Switch traffic
        run: |
          kubectl patch service app \
            -n production \
            -p '{"spec":{"selector":{"version":"green"}}}'

      - name: Cleanup blue
        run: kubectl delete -f k8s/blue/ || true
```

### Canary デプロイ

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy canary (10%)
        run: |
          kubectl apply -f k8s/canary/
          kubectl scale deployment/app-canary --replicas=1

      - name: Monitor
        run: |
          sleep 300
          # Check error rate, latency, etc.

      - name: Promote or rollback
        run: |
          if [ "${{ job.status }}" == "success" ]; then
            kubectl set image deployment/app app=$IMAGE
            kubectl delete -f k8s/canary/
          else
            kubectl delete -f k8s/canary/
            exit 1
          fi
```

### Rolling Update（Kubernetes標準）

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    spec:
      containers:
        - name: app
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 10
```
