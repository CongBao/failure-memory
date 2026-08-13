package cli

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	embeddingadapter "github.com/CongBao/failure-memory/internal/adapters/embedding"
	"github.com/CongBao/failure-memory/internal/config"
	"github.com/CongBao/failure-memory/internal/hook"
	"github.com/CongBao/failure-memory/internal/install"
	"github.com/CongBao/failure-memory/internal/mcpserver"
	"github.com/CongBao/failure-memory/internal/model"
	"github.com/CongBao/failure-memory/internal/service"
	storesqlite "github.com/CongBao/failure-memory/internal/store/sqlite"
	"github.com/CongBao/failure-memory/internal/version"
)

func Run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		printUsage(stderr)
		return 2
	}
	switch args[0] {
	case "version", "--version", "-version":
		_, _ = fmt.Fprintf(stdout, "failure-memory %s (%s, %s)\n", version.Version, version.Commit, version.Date)
		return 0
	case "mcp":
		return runMCP(args[1:], stderr)
	case "remember":
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			var input model.RememberInput
			if err := decodeOne(stdin, &input); err != nil {
				return err
			}
			result, err := svc.Remember(ctx, input)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "recall":
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			var input model.RecallInput
			if err := decodeOne(stdin, &input); err != nil {
				return err
			}
			result, err := svc.Recall(ctx, input)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "outcome":
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			var input model.MemoryOutcomeInput
			if err := decodeOne(stdin, &input); err != nil {
				return err
			}
			result, err := svc.ReportOutcome(ctx, input)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "doctor":
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			result, err := svc.Doctor(ctx)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "metrics":
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			result, err := svc.Metrics(ctx)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "store-status":
		return withService("cli", stderr, func(_ context.Context, svc *service.Service) error {
			return encodeOne(stdout, svc.StoreStatus())
		})
	case "index":
		if len(args) != 2 || args[1] != "build" {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory index build")
			return 2
		}
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			result, err := svc.RebuildIndex(ctx)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "adapters":
		return runAdapters(args[1:], stdout, stderr)
	case "cluster":
		return runCluster(args[1:], stdin, stdout, stderr)
	case "migrate-v0":
		return runMigrateV0(args[1:], stdout, stderr)
	case "hook":
		return runHook(args[1:], stdin, stdout, stderr)
	case "install":
		return runInstall(args[1:], stdout, stderr)
	case "backup":
		return runBackup(args[1:], stdout, stderr)
	case "help", "--help", "-h":
		printUsage(stdout)
		return 0
	default:
		_, _ = fmt.Fprintf(stderr, "unknown command %q\n", args[0])
		printUsage(stderr)
		return 2
	}
}

func runBackup(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory backup <create|verify|restore>")
		return 2
	}
	paths, err := config.ResolvePaths()
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	switch args[0] {
	case "create":
		flags := flag.NewFlagSet("backup create", flag.ContinueOnError)
		flags.SetOutput(stderr)
		output := flags.String("output", "", "new backup directory")
		if err := flags.Parse(args[1:]); err != nil {
			return 2
		}
		if flags.NArg() != 0 {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory backup create [--output <directory>]")
			return 2
		}
		if strings.TrimSpace(*output) == "" {
			*output = filepath.Join(
				paths.Root,
				"backups",
				"failure-memory-"+time.Now().UTC().Format("20060102T150405.000000000Z"),
			)
		}
		return withService("backup", stderr, func(ctx context.Context, svc *service.Service) error {
			result, err := svc.CreateBackup(ctx, *output)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "verify":
		if len(args) != 2 {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory backup verify <directory>")
			return 2
		}
		result, err := storesqlite.VerifyBackup(context.Background(), args[1])
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
			return 1
		}
		if err := encodeOne(stdout, result); err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
			return 1
		}
		return 0
	case "restore":
		var backupPath string
		if len(args) == 3 && args[1] == "--replace" {
			backupPath = args[2]
		} else if len(args) == 3 && args[2] == "--replace" {
			backupPath = args[1]
		} else {
			_, _ = fmt.Fprintln(
				stderr,
				"usage: failure-memory backup restore <directory> --replace",
			)
			return 2
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		result, err := storesqlite.RestoreBackup(
			ctx,
			paths.EventStore,
			backupPath,
			filepath.Join(paths.Root, "backups"),
		)
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
			return 1
		}
		svc, err := service.Open("restore")
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: event store restored but reopen failed: %v\n", err)
			return 1
		}
		rebuild, rebuildErr := svc.RebuildIndex(ctx)
		closeErr := svc.Close()
		if err := errors.Join(rebuildErr, closeErr); err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: event store restored but index rebuild failed: %v\n", err)
			return 1
		}
		if err := encodeOne(stdout, map[string]any{
			"restore":       result,
			"index_rebuild": rebuild,
		}); err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
			return 1
		}
		return 0
	default:
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory backup <create|verify|restore>")
		return 2
	}
}

func runInstall(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory install <status|runtime|plugin|all>")
		return 2
	}
	paths, err := config.ResolvePaths()
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	var output any
	switch args[0] {
	case "status":
		if len(args) != 1 {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory install status")
			return 2
		}
		output, err = install.Inspect(paths)
	case "runtime":
		if len(args) != 1 {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory install runtime")
			return 2
		}
		output, err = install.InstallRuntime(paths)
	case "plugin", "all":
		flags := flag.NewFlagSet("install "+args[0], flag.ContinueOnError)
		flags.SetOutput(stderr)
		var harnesses []string
		flags.Func("harness", "codex, claude, copilot, cursor, or auto; repeatable", func(value string) error {
			harnesses = append(harnesses, value)
			return nil
		})
		if parseErr := flags.Parse(args[1:]); parseErr != nil {
			return 2
		}
		if flags.NArg() != 0 {
			_, _ = fmt.Fprintf(stderr, "usage: failure-memory install %s [--harness <name>]\n", args[0])
			return 2
		}
		if args[0] == "plugin" {
			output, err = install.InstallPlugins(context.Background(), harnesses)
		} else {
			output, err = install.InstallAll(context.Background(), paths, harnesses)
		}
	default:
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory install <status|runtime|plugin|all>")
		return 2
	}
	if output != nil {
		if encodeErr := encodeOne(stdout, output); encodeErr != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", encodeErr)
			return 1
		}
	}
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	return 0
}

func runHook(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("hook", flag.ContinueOnError)
	flags.SetOutput(stderr)
	harness := flags.String("harness", "", "host harness")
	event := flags.String(
		"event", "", "session-start, user-prompt-submit, or user-prompt-transformed",
	)
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() != 0 || *harness == "" || *event == "" {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory hook --harness <name> --event <name>")
		return 2
	}
	output, emit, err := hook.Run(*harness, *event, stdin)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 2
	}
	if !emit {
		return 0
	}
	if err := encodeOne(stdout, output); err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	return 0
}

func runMigrateV0(args []string, stdout, stderr io.Writer) int {
	flags := flag.NewFlagSet("migrate-v0", flag.ContinueOnError)
	flags.SetOutput(stderr)
	source := flags.String("source", "", "path to a v0.3-v0.7 SQLite store snapshot")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() != 0 || strings.TrimSpace(*source) == "" {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory migrate-v0 --source <sqlite-snapshot>")
		return 2
	}
	return withService("migration", stderr, func(ctx context.Context, svc *service.Service) error {
		result, err := svc.MigrateV07(ctx, *source)
		if err != nil {
			return err
		}
		return encodeOne(stdout, result)
	})
}

func runCluster(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory cluster <propose|review>")
		return 2
	}
	switch args[0] {
	case "propose":
		flags := flag.NewFlagSet("cluster propose", flag.ContinueOnError)
		flags.SetOutput(stderr)
		threshold := flags.Float64("threshold", 0.18, "maximum cosine distance")
		if err := flags.Parse(args[1:]); err != nil || flags.NArg() != 0 {
			return 2
		}
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			result, err := svc.ProposeClusters(ctx, *threshold)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	case "review":
		if len(args) != 1 {
			_, _ = fmt.Fprintln(stderr, "usage: failure-memory cluster review")
			return 2
		}
		return withService("cli", stderr, func(ctx context.Context, svc *service.Service) error {
			var input model.GeneralizationReviewInput
			if err := decodeOne(stdin, &input); err != nil {
				return err
			}
			result, err := svc.ReviewGeneralization(ctx, input)
			if err != nil {
				return err
			}
			return encodeOne(stdout, result)
		})
	default:
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory cluster <propose|review>")
		return 2
	}
}

func runAdapters(args []string, stdout, stderr io.Writer) int {
	if len(args) != 1 || (args[0] != "status" && args[0] != "install") {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory adapters <status|install>")
		return 2
	}
	paths, err := config.ResolvePaths()
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	if args[0] == "status" {
		if err := encodeOne(stdout, embeddingadapter.StatusAt(paths.EmbeddingModel)); err != nil {
			_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
			return 1
		}
		return 0
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	status, err := embeddingadapter.Install(ctx, paths.EmbeddingModel)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	if err := encodeOne(stdout, status); err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	return 0
}

func runMCP(args []string, stderr io.Writer) int {
	flags := flag.NewFlagSet("mcp", flag.ContinueOnError)
	flags.SetOutput(stderr)
	stdio := flags.Bool("stdio", false, "serve MCP over standard input/output")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if !*stdio || flags.NArg() != 0 {
		_, _ = fmt.Fprintln(stderr, "usage: failure-memory mcp --stdio")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	svc, err := service.Open("mcp")
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	defer func() { _ = svc.Close() }()
	if err := mcpserver.Run(ctx, svc); err != nil && !errors.Is(err, context.Canceled) {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	return 0
}

func withService(
	transport string,
	stderr io.Writer,
	run func(context.Context, *service.Service) error,
) int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	svc, err := service.Open(transport)
	if err == nil {
		defer func() { _ = svc.Close() }()
		err = run(ctx, svc)
	}
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "failure-memory: %v\n", err)
		return 1
	}
	return 0
}

func decodeOne(reader io.Reader, destination any) error {
	decoder := json.NewDecoder(io.LimitReader(reader, 1<<20))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return fmt.Errorf("invalid JSON input: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("input must contain one JSON object")
		}
		return fmt.Errorf("invalid trailing input: %w", err)
	}
	return nil
}

func encodeOne(writer io.Writer, value any) error {
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}

func printUsage(writer io.Writer) {
	lines := []string{
		"usage: failure-memory <command>",
		"",
		"Agent operations:",
		"  mcp --stdio       serve the three public MCP tools",
		"  remember          read one remember_failure JSON object from stdin",
		"  recall            read one recall_failure_lessons JSON object from stdin",
		"  outcome           append one evidence-bounded memory outcome",
		"",
		"Administration:",
		"  doctor            verify the global store and retrieval adapter",
		"  metrics           print append-only event counts",
		"  store-status      print the shared store identity",
		"  index build       rebuild derived exact, FTS5, and vector indexes",
		"  adapters status   show the optional semantic embedding adapter",
		"  adapters install  explicitly install the pinned semantic model",
		"  cluster propose   create non-mutating generalization proposals",
		"  cluster review    append an accept/reject/defer proposal review",
		"  migrate-v0        copy a reviewed v0.3-v0.7 store into v1",
		"  hook              emit bounded non-persistent harness guidance",
		"  install status    detect one runtime and duplicate plugin identities",
		"  install runtime   install/update the shared native executable",
		"  install plugin    install/update harness plugins (--harness defaults to auto)",
		"  install all       install runtime and detected harness plugins in one operation",
		"  backup create     create a verified snapshot of the authoritative event store",
		"  backup verify     verify a backup manifest, checksum, database, and event hashes",
		"  backup restore    safely replace the global store and rebuild derived indexes",
		"  version           print build identity",
	}
	_, _ = fmt.Fprintln(writer, strings.Join(lines, "\n"))
}
