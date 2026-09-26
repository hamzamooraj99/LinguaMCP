using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;

namespace LinguaMCP.Launcher;

public partial class MainWindow : Window
{
    private readonly string _projectRoot;
    private readonly string _pythonPath;
    private readonly string _serverPath;
    private readonly string _statePath;
    private readonly string _logPath;
    private readonly DispatcherTimer _timer;
    private readonly object _logLock = new();
    private Process? _server;
    private StreamWriter? _logWriter;

    public MainWindow()
    {
        InitializeComponent();
        _projectRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", ".."));
        _pythonPath = Path.Combine(_projectRoot, ".venv", "Scripts", "python.exe");
        _serverPath = Path.Combine(_projectRoot, "server.py");
        var dataRoot = Path.Combine(_projectRoot, "tutor_data");
        _statePath = Path.Combine(dataRoot, ".launcher-state.json");
        _logPath = Path.Combine(dataRoot, "launcher-server.log");
        Directory.CreateDirectory(dataRoot);

        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _timer.Tick += (_, _) => RefreshState();
        _timer.Start();
        RefreshState();
    }

    private int? ReadPid()
    {
        try
        {
            var state = JsonSerializer.Deserialize<LauncherState>(File.ReadAllText(_statePath));
            if (state is null) return null;
            using var process = Process.GetProcessById(state.Pid);
            return process.HasExited ? null : state.Pid;
        }
        catch
        {
            return null;
        }
    }

    private void RefreshState()
    {
        if (_server is { HasExited: true })
        {
            _server = null;
            CloseLog();
            File.Delete(_statePath);
            SetStatus("Error", "The server stopped unexpectedly. Check the output below.", "#B42318", true, false);
        }
        else if (ReadPid() is not null)
        {
            SetStatus("Running", "OAuth MCP endpoint is available on port 8000.", "#178A4B", false, true);
        }
        else
        {
            SetStatus("Stopped", "The MCP server is offline.", "#8A9099", true, false);
        }

        if (File.Exists(_logPath))
        {
            try
            {
                using var stream = new FileStream(_logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                using var reader = new StreamReader(stream);
                var lines = reader.ReadToEnd().Split(Environment.NewLine).TakeLast(80);
                LogText.Text = string.Join(Environment.NewLine, lines);
                LogText.ScrollToEnd();
            }
            catch (IOException)
            {
                // A log refresh should never take down the launcher.
            }
        }
    }

    private void SetStatus(string title, string detail, string color, bool canStart, bool canStop)
    {
        StatusTitle.Text = title;
        StatusDetail.Text = detail;
        StatusDot.Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
        StartButton.IsEnabled = canStart;
        StopButton.IsEnabled = canStop;
    }

    private void StartButton_Click(object sender, RoutedEventArgs e)
    {
        if (ReadPid() is not null) return;
        if (!File.Exists(_pythonPath) || !File.Exists(_serverPath))
        {
            MessageBox.Show("Run setup_launcher.cmd once before starting the server.", "Setup required", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        if (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("LINGUAMCP_OAUTH_PASSWORD")))
        {
            MessageBox.Show("Set LINGUAMCP_OAUTH_PASSWORD as a Windows user environment variable, then reopen the launcher.", "OAuth password missing", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        var oauthClientsFile = Environment.GetEnvironmentVariable("LINGUAMCP_OAUTH_CLIENTS_FILE");
        if (string.IsNullOrWhiteSpace(oauthClientsFile) || !File.Exists(oauthClientsFile))
        {
            MessageBox.Show("Set LINGUAMCP_OAUTH_CLIENTS_FILE to a deployment-owned JSON registry of approved client IDs and exact callback URLs, then reopen the launcher.", "OAuth client registry missing", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        if (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("LINGUAMCP_PUBLIC_BASE_URL")))
        {
            MessageBox.Show("Set LINGUAMCP_PUBLIC_BASE_URL to the server's canonical HTTPS origin, then reopen the launcher.", "OAuth public URL missing", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        SetStatus("Starting", "FastMCP is starting up...", "#FF5A00", false, false);
        lock (_logLock)
        {
            _logWriter = new StreamWriter(new FileStream(_logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite)) { AutoFlush = true };
            _logWriter.WriteLine();
            _logWriter.WriteLine("--- Starting LinguaMCP MCP ---");
        }

        _server = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = _pythonPath,
                Arguments = $"\"{_serverPath}\" --http --oauth --allow-writes",
                WorkingDirectory = _projectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            },
            EnableRaisingEvents = true,
        };
        _server.OutputDataReceived += (_, args) => AppendLog(args.Data);
        _server.ErrorDataReceived += (_, args) => AppendLog(args.Data);
        _server.Start();
        _server.BeginOutputReadLine();
        _server.BeginErrorReadLine();
        File.WriteAllText(_statePath, JsonSerializer.Serialize(new LauncherState(_server.Id)));
        RefreshState();
    }

    private void StopButton_Click(object sender, RoutedEventArgs e)
    {
        var pid = ReadPid();
        if (pid is null) return;
        try
        {
            using var process = Process.GetProcessById(pid.Value);
            process.Kill(true);
            process.WaitForExit(5000);
        }
        catch { }
        _server = null;
        AppendLog("--- LinguaMCP MCP stopped ---");
        CloseLog();
        File.Delete(_statePath);
        RefreshState();
    }

    private void OpenLog_Click(object sender, RoutedEventArgs e)
    {
        if (!File.Exists(_logPath)) File.WriteAllText(_logPath, string.Empty);
        Process.Start(new ProcessStartInfo(_logPath) { UseShellExecute = true });
    }

    private void AppendLog(string? line)
    {
        if (line is null) return;
        lock (_logLock)
        {
            _logWriter?.WriteLine(line);
        }
    }

    private void CloseLog()
    {
        lock (_logLock)
        {
            _logWriter?.Dispose();
            _logWriter = null;
        }
    }

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        if (ReadPid() is not null) StopButton_Click(this, new RoutedEventArgs());
        _timer.Stop();
        base.OnClosing(e);
    }

    private sealed record LauncherState(int Pid);
}
