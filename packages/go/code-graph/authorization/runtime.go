package authorization

import (
	"context"
	"log/slog"
	"os"
	"time"
)

type Config struct {
	Mode           string        `mapstructure:"mode"`
	LocalTenantID  string        `mapstructure:"local_tenant_id"`
	MySQLDSN       string        `mapstructure:"mysql_dsn"`
	PollInterval   time.Duration `mapstructure:"poll_interval"`
	MaxAge         time.Duration `mapstructure:"max_age"`
	RequestTimeout time.Duration `mapstructure:"request_timeout"`
}

func DefaultConfig() Config {
	d := DefaultOptions()
	return Config{PollInterval: d.PollInterval, MaxAge: d.MaxAge, RequestTimeout: d.RequestTimeout}
}
func (c Config) Open(ctx context.Context, logger *slog.Logger) (*Store, *Service, error) {
	if c.Mode != "" && c.Mode != "corporate" && c.Mode != "local-development" {
		return nil, nil, ErrInvalid
	}
	if c.Mode == "local-development" && !identifier.MatchString(c.LocalTenantID) {
		return nil, nil, ErrInvalid
	}
	if os.Getenv("CODEGRAPH_AUTHORIZATION_MODE") == "local-development" && logger != nil {
		logger.Warn("local development auto-login enabled", "tenant", os.Getenv("CODEGRAPH_AUTHORIZATION_LOCAL_TENANT_ID"))
	}
	store, err := OpenMySQL(c.MySQLDSN)
	if err != nil {
		return nil, nil, err
	}
	service, err := NewService(store, Options{c.PollInterval, c.MaxAge, c.RequestTimeout}, logger)
	if err != nil {
		_ = store.Close()
		return nil, nil, err
	}
	check, cancel := context.WithTimeout(ctx, c.RequestTimeout)
	defer cancel()
	if err = service.Refresh(check); err != nil {
		_ = store.Close()
		return nil, nil, err
	}
	go service.Run(ctx)
	return store, service, nil
}
