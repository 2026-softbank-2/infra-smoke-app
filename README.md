# infra-smoke-app

AWS·GCP 배포 및 트래픽 전환 검증용 최소 FastAPI 애플리케이션입니다.
Hunter의 ECS Fargate·Cloud Run에서 동일한 컨테이너 이미지를 실행합니다.
DB·Redis·파일 저장소·클라우드 SDK 없이 동작하며, 관제 기능은 `hunter-platform`, 인프라 구성은 `hunter-infra`에서 관리합니다.

## HTTP API

| 경로 | 응답 | 용도 |
|---|---|---|
| `GET /health` | `{"status":"ok","env":"aws"}` | 앱 HTTP 응답 확인: HAProxy·AWS ALB 헬스체크와 Cloud Run 시작 검사 |
| `GET /` | 서비스 이름, 실행 환경, 버전, 인스턴스 식별자 | 실제 응답 클라우드·배포 버전·프로세스 구분 |

루트 응답 예시:

```json
{
  "service": "infra-smoke-app",
  "env": "aws",
  "version": "v0.1.0",
  "instance_id": "ef1d67df6289447090c1c7452cce3406"
}
```

두 경로는 `200`과 `Cache-Control: no-store`를 반환합니다. `instance_id`는 프로세스 시작마다 생성되며 같은 프로세스의 요청에서는 유지됩니다. `/health`는 앱 응답만 검사하며 DB·캐시·파일 저장소 준비 상태를 보장하지 않습니다.

## 로컬 실행

Python 3.13 기준입니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

다른 터미널에서:

```bash
curl --fail-with-body http://localhost:8080/health
curl --fail-with-body http://localhost:8080/
```

| 환경변수 | 기본값·동작 |
|---|---|
| `PORT` | `8080`. 항상 `0.0.0.0`에서 HTTP 수신 |
| `APP_ENV` | 선택적 수동 표시값. 없으면 `K_SERVICE`가 있는 Cloud Run은 `gcp`, `AWS_EXECUTION_ENV=AWS_ECS_*`인 ECS는 `aws`, 그 밖에는 `local` |
| `APP_VERSION` | 기본 `dev`. Docker 빌드 인수로 이미지에 기록하거나 실행 시 설정 |

Terraform의 `applications.*.environment`는 양쪽 클라우드가 공유하므로 **여기에 `APP_ENV=aws` 또는 `gcp`를 지정하지 않습니다.** 자동 감지 덕분에 현재 Terraform 모듈을 수정하지 않고 같은 이미지로 실행 위치를 구분합니다. 감지는 환경변수의 존재·값만 읽으며 메타데이터 API나 클라우드 API를 호출하지 않습니다.

## 컨테이너

현재 Terraform의 ECS CPU 아키텍처가 `X86_64`이고 Cloud Run도 `linux/amd64` 이미지를 지원하므로, Apple Silicon에서도 배포 이미지는 명시적으로 `linux/amd64`로 빌드합니다.

```bash
docker build --platform linux/amd64 \
  --build-arg APP_VERSION=v0.1.0 \
  -t infra-smoke-app:v0.1.0 .

docker run --rm --platform linux/amd64 \
  -p 127.0.0.1:8080:8080 \
  -e APP_ENV=local \
  infra-smoke-app:v0.1.0
```

컨테이너는 UID/GID `10001`로 단일 프로세스를 실행하며 로그를 표준 출력·오류로 보냅니다. Python 프로세스가 직접 SIGTERM을 받아 종료합니다. TLS는 인프라에서 처리하며 앱은 HTTP만 제공합니다.

## hunter-infra 연결

[`deploy/terraform.tfvars.example`](deploy/terraform.tfvars.example)의 `smoke` 항목을 **기존 환경 입력의 `applications` map에 추가**합니다. Terraform의 map 입력은 통째로 교체되므로 별도 `-var-file`로 덮어쓰면 기존 앱이 제거될 수 있습니다.

1. 같은 `linux/amd64` 이미지를 한 번 빌드해 ECR과 Artifact Registry에 각각 태그·푸시합니다. 클라우드별로 다시 빌드하지 않습니다.
2. 양쪽에 게시된 이미지의 주소와 digest를 확인해 `aws_image`, `gcp_image`에 지정합니다.
3. `port = 8080`, `health_path = "/health"`를 사용합니다. 앱 키가 `smoke`이고 도메인이 `sbhunter.site`이면 기본 호스트는 `smoke.sbhunter.site`입니다.
4. `hunter-infra`에서 같은 backend·state와 기존 환경 입력을 유지해 `./scripts/plan.sh services terraform.tfvars`로 전체 계획을 검토합니다.
5. 인프라·이미지 배포와 DNS·TLS 준비가 끝나면 공개 호스트로 확인합니다.

```bash
curl --fail-with-body https://smoke.sbhunter.site/health
curl --fail-with-body https://smoke.sbhunter.site/

# 요청별 env를 확인해 가중치 변경 결과를 관찰합니다.
for i in $(seq 1 20); do
  curl --fail-with-body --silent --show-error https://smoke.sbhunter.site/
  printf '\n'
done
```

`env`는 최종 앱의 실행 클라우드를 나타내며, 앞에서 요청을 받은 HAProxy의 위치는 나타내지 않습니다. 소수의 요청이 설정 가중치와 정확히 같은 비율로 나오지는 않을 수 있습니다.

현재 `foundation` 단계에는 앱 런타임·이미지 저장소가 없습니다. `services` 단계는 앱과 함께 Redis·파일 저장소·비밀 등의 리소스도 포함하며, VM 교체가 계획될 수 있습니다. 이미지 저장소가 아직 없다면 이미지 게시 순서도 인프라 담당자와 맞춰야 합니다. 이 저장소를 만드는 것만으로 클라우드 리소스·DNS가 생성되거나 앱이 배포되지는 않습니다.

Terraform에서 주입하는 `CACHE_*`, `FILES_*` 값은 이 앱이 사용하거나 응답에 노출하지 않습니다. 앱이 이를 사용하지 않아도 현재 Terraform이 연결한 비밀은 런타임 시작을 위해 조회 가능해야 합니다.

## 의존성 갱신

`requirements.in`에서 직접 의존성 버전을 관리하고, `requirements.txt`에 전이 의존성까지 고정합니다. 갱신할 때만 `uv`를 사용하며 앱 실행·Docker 빌드에는 필요하지 않습니다.

```bash
uv pip compile requirements.in --python-version 3.13 --universal \
  --no-header --no-annotate -o requirements.txt
```

## 연결 규약 근거

- [hunter-infra 앱 입력](https://github.com/2026-softbank-2/hunter-infra/blob/main/variables.tf)
- [hunter-infra 런타임 계약](https://github.com/2026-softbank-2/hunter-infra/blob/main/docs/runtime-contract.md)
- [Cloud Run 컨테이너 규약](https://docs.cloud.google.com/run/docs/container-contract): `PORT`, `K_SERVICE`, 수신 주소와 이미지 아키텍처
- [ECS 환경변수](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs-environment-variables.html): `AWS_EXECUTION_ENV`
- [FastAPI 컨테이너 배포](https://fastapi.tiangolo.com/deployment/docker/): 실행 명령과 종료 신호 처리
