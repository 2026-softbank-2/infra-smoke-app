# infra-smoke-app

AWS·GCP 배포 및 트래픽 전환 검증용 최소 FastAPI 애플리케이션입니다.
Hunter의 ECS Fargate·Cloud Run에서 동일한 컨테이너 이미지를 실행합니다.
Redis·PostgreSQL은 선택적으로 연결하며, 미설정 상태에서도 기본 HTTP 응답을 제공합니다. 파일 저장소·클라우드 SDK는 포함하지 않습니다. 관제 기능은 `hunter-platform`, 인프라 구성은 `hunter-infra`에서 관리합니다.

## HTTP API

| 경로 | 응답 | 용도 |
|---|---|---|
| `GET /health` | `{"status":"ok","env":"aws"}` | 앱 HTTP 응답 확인: HAProxy·AWS ALB 헬스체크와 Cloud Run 시작 검사 |
| `GET /` | 서비스 이름, 실행 환경, 버전, 인스턴스 식별자 | 실제 응답 클라우드·배포 버전·프로세스 구분 |
| `GET /cache/health` | Redis PING 성공 시 200, 미설정·연결 실패 시 503 | TLS·인증·연결 확인 |
| `POST /cache/check` | PING·SET·GET·TTL·실제 만료 검사 결과 | 전부 통과하면 200, 실패·미설정이면 503 |
| `GET /cache/items/1` | 고정 샘플과 `cache`·`source` | miss → hit, 캐시 장애 시 fixture 응답 |
| `GET /db/health`, `POST /db/check` | 쿼리·TLS·읽기 전용 여부 | 기존 PostgreSQL의 실제 연결 확인 |
| `GET /db/products` | 최대 10개 상품 | 명시적으로 켠 경우 기존 Hunter 데모 DB 조회 |

루트 응답 예시:

```json
{
  "service": "infra-smoke-app",
  "env": "aws",
  "version": "v0.1.0",
  "instance_id": "ef1d67df6289447090c1c7452cce3406"
}
```

두 기본 경로는 `200`과 `Cache-Control: no-store`를 반환합니다. `instance_id`는 프로세스 시작마다 생성되며 같은 프로세스의 요청에서는 유지됩니다. `/health`는 앱 응답만 검사하며 DB·캐시·파일 저장소 준비 상태를 보장하지 않습니다.

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

`hunter-infra`의 `registries` 단계에서 `application_repository_names = ["smoke"]`로 저장소를 먼저 준비합니다. 이미지 게시 후 `services`로 전환하며, `enable_file_services = false`이면 앱·Redis만 추가하고 파일 저장소·게이트웨이·복제 권한은 제외합니다. 단계 변경 시 VM 교체가 계획될 수 있으므로 실제 state의 전체 plan을 검토합니다.

## Redis 연결과 검사

Terraform이 주입하는 기존 규약을 사용합니다. 비밀번호와 CA는 각 클라우드의 비밀 저장소에서 컨테이너에 주입하며 앱 응답·로그에 노출하지 않습니다.

| 환경변수 | 동작 |
|---|---|
| `CACHE_HOST` | 미설정이면 캐시 검사는 503, 기본 HTTP·fixture 응답은 정상 |
| `CACHE_PORT` | 기본 6379, GCP는 실제 인스턴스의 포트 주입 |
| `CACHE_TLS` | `true`만 허용, 서버 인증서와 호스트명 검증 |
| `CACHE_PASSWORD` | 캐시 설정 시 필수 |
| `CACHE_CA_CERT` | GCP Redis의 PEM CA 묶음. 미설정 시 시스템 신뢰 저장소 사용 |

`POST /cache/check`는 요청마다 고유 키와 값을 생성해 PING·조건부 SET·동일 값 GET·양수 TTL·실제 만료를 검사합니다. 키 TTL은 1초, 전체 검사는 최대 5초이며 동시 실행은 프로세스당 4개입니다. 기존 키 삭제·전체 flush·임의 키 조회는 제공하지 않습니다.

`GET /cache/items/{1..3}`은 60초 TTL로 고정 샘플을 캐시합니다. 처음에는 `cache=miss, source=fixture`, 이후 `cache=hit, source=cache`입니다. 연결 실패는 `cache=unavailable`, 비정상 값은 `cache=invalid`로 표시하며 안전한 fixture로 HTTP 200을 반환합니다. `/health`는 캐시 장애로 실패하지 않습니다.

AWS와 GCP의 Redis는 독립 캐시이며 복제·이전하지 않습니다. 전환 후 빈 캐시는 같은 fixture에서 다시 채웁니다. fixture는 업무 DB·세션·주문 저장소가 아닙니다.

```bash
curl --fail-with-body https://smoke.sbhunter.site/cache/health
curl --fail-with-body -X POST https://smoke.sbhunter.site/cache/check
curl --fail-with-body https://smoke.sbhunter.site/cache/items/1
```

로컬 검증은 Docker·OpenSSL·Python 3.13 환경에서 실행합니다. 실제 TLS Redis와 잘못된 인증정보·CA·호스트명, 연결 중단·복구까지 검사하고 테스트 컨테이너를 제거합니다.

```bash
uv pip sync --python .venv/bin/python requirements-dev.txt
bash scripts/test-cache.sh
```


## 온프렘과 PostgreSQL

온프렘에서는 `APP_ENV=onprem`으로 표시하며 ARM64 이미지를 빌드합니다. VM·K3s·Redis·Tunnel은 [hunter-infra](https://github.com/2026-softbank-2/hunter-infra)의 `onprem/`에서 관리합니다. 온프렘 PostgreSQL은 새로 생성하지 않고 기존 클라우드 DB를 사용합니다.

| 환경변수 | 동작 |
|---|---|
| `DB_HOST` | 미설정이면 DB API는 503. 기본 HTTP·캐시 기능은 정상 |
| `DB_HOSTADDR` | 선택적 접속 IP. 사설 릴레이를 사용해도 TLS는 `DB_HOST`의 원래 서버 이름으로 검증 |
| `DB_PORT` | 기본 5432 |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | 기존 DB의 앱 계정. 비밀번호는 Secret으로 전달 |
| `DB_SSLMODE` | `verify-full`만 허용 |
| `DB_CA_FILE` | 마운트한 CA 인증서 묶음의 경로 |
| `DB_SAMPLE_CATALOG` | `true`일 때만 기존 `products` 테이블의 최대 10개 행 조회 |

DB 요청은 연결부터 종료까지 6초, 동시 연결 4개로 제한합니다. 서버의 `default_transaction_read_only=on`, 쿼리 제한 2초, 잠금 대기 제한 1초를 적용합니다. `/db/check`는 실제 쿼리와 `pg_stat_ssl`로 TLS·읽기 전용 상태를 확인합니다. 자동 스키마 생성·마이그레이션·테이블 쓰기는 수행하지 않습니다. DB 장애·인증 오류는 503으로 구분하고, 오류 원문·접속 주소·비밀번호는 응답에 담지 않습니다. `/health`는 독립적으로 200을 유지합니다.

DB 검사는 연결·조회 검증이며 데이터 최신성·CDC 동기화·업무 계정의 쓰기 전환 완료를 의미하지 않습니다. 기존 배포에 `DB_HOST`를 추가하지 않으면 이전 HTTP·캐시 동작을 유지합니다.

```bash
bash scripts/test-database.sh
```

이 검사는 소유한 임시 TLS PostgreSQL 컨테이너에서 실제 조회, 잘못된 비밀번호·CA·호스트명 거부, 서버의 쓰기 차단과 연결 복구를 확인하고 컨테이너를 제거합니다. 클라우드 DB를 테스트 fixture로 사용하지 않습니다.

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

## 카나리 관제 계약

`GET /ready`는 Redis와 설정된 PostgreSQL이 준비됐을 때 200을 반환합니다. 프로세스 생존 검사는 `/health`를 사용하고, 트래픽 수신 자격은 `/ready`로 확인합니다. `GET /runtime`은 `env`, `workload`, `version`, `database_binding`, `database_identity`를 반환합니다.

- `APP_WORKLOAD`: 기본 `smoke`. 앱마다 고유한 지표 식별자를 설정합니다.
- `DB_BINDING`: 공유 PostgreSQL의 운영 식별자입니다. DB 설정 시 기본 `unverified`이므로 관제 등록값과 별도로 맞춰야 합니다.
- `database_identity`: TLS로 인증하는 `DB_HOST`와 `DB_NAME`의 SHA-256 지문입니다. 계산 입력은 compact ASCII JSON `[DB_HOST.lower().rstrip("."), DB_NAME]`이며 비밀번호·릴레이 IP·포트는 포함하지 않습니다.
- `GET /metrics`: `http_server_requests_seconds` histogram과 `hunter_runtime_info`를 Prometheus 형식으로 제공합니다. 메서드·라우트 템플릿·상태 코드·workload만 라벨로 사용하며 URL 쿼리·임의 경로·요청 헤더는 수집하지 않습니다.
- `METRICS_BEARER_TOKEN`: 설정하면 `/metrics`에 동일한 Bearer 토큰을 요구합니다. 수집 토큰은 비밀 저장소에서 주입합니다.

플랫폼 공유 DB 이전은 등록된 DB 지문·DB 식별자·`APP_VERSION`을 출발/대상 런타임과 대조하고 `/ready`를 확인합니다. 런타임 메타데이터는 선언한 설정이며, 이미지 digest와 소스 일치는 배포 절차에서 별도로 검증합니다. 이 변경을 사용하려면 같은 검증된 이미지를 각 환경에 배포하고 workload/env별 수집 대상을 연결해야 합니다.
