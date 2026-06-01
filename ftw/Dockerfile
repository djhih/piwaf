# 建置 go-ftw。CRS 回歸測試集已 vendor 在 ./crs-tests（對齊 OWASP_CRS v4.12.0）。
# （不在映像內 git clone：本機 Docker build 網路會 reset github，故改用 build context 帶入）
FROM golang:1.24-alpine AS build
RUN apk add --no-cache git
RUN go install github.com/coreruleset/go-ftw@latest

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /go/bin/go-ftw /usr/local/bin/ftw
COPY crs-tests /tests
WORKDIR /work
ENTRYPOINT ["ftw"]
# 預設跑全套；縮範圍可在 run 時覆寫，例：
#   docker-compose run --rm ftw run -d /tests --config /etc/ftw/config.yaml -i 942
CMD ["run", "-d", "/tests", "--config", "/etc/ftw/config.yaml"]
