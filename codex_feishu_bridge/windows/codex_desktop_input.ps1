param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('draft', 'clear', 'submit', 'stop')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$ThreadId,

    [string]$TextBase64 = '',

    [string]$AttachmentsBase64 = '',

    [int]$TimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Accessibility
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies Microsoft.CSharp @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class CodexDesktopNative
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public UIntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT
    {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public UIntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct INPUTUNION
    {
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public MOUSEINPUT mi;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT
    {
        public uint type;
        public INPUTUNION U;
    }

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(
        EnumWindowsProc lpEnumFunc,
        IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumChildWindows(
        IntPtr hWndParent,
        EnumWindowsProc lpEnumFunc,
        IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(
        IntPtr hWnd,
        out uint lpdwProcessId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(
        IntPtr hWnd,
        System.Text.StringBuilder lpClassName,
        int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(
        uint idAttach,
        uint idAttachTo,
        bool fAttach);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern uint GetClipboardSequenceNumber();

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SystemParametersInfo(
        uint uiAction,
        uint uiParam,
        ref bool pvParam,
        uint fWinIni);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern uint SendInput(
        uint nInputs,
        INPUT[] pInputs,
        int cbSize);

    [DllImport("oleacc.dll")]
    public static extern int AccessibleObjectFromWindow(
        IntPtr hwnd,
        uint dwObjectID,
        ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out object ppvObject);

    public static IntPtr[] Children(IntPtr parent)
    {
        var result = new List<IntPtr>();
        EnumChildWindows(parent, (handle, _) =>
        {
            result.Add(handle);
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }

    public static IntPtr[] TopLevelWindows(uint processId)
    {
        var result = new List<IntPtr>();
        EnumWindows((handle, _) =>
        {
            uint owner;
            GetWindowThreadProcessId(handle, out owner);
            if (owner == processId)
            {
                result.Add(handle);
            }
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }

    public static string ClassName(IntPtr handle)
    {
        var buffer = new System.Text.StringBuilder(256);
        GetClassName(handle, buffer, buffer.Capacity);
        return buffer.ToString();
    }

    public static object Accessible(IntPtr handle)
    {
        var iid = new Guid("618736E0-3C3D-11CF-810C-00AA00389B71");
        object result;
        var hresult = AccessibleObjectFromWindow(
            handle,
            unchecked((uint)-4),
            ref iid,
            out result);
        return hresult == 0 ? result : null;
    }

    public static object FindAccessible(
        object root,
        int expectedRole,
        string[] acceptedNames,
        int maxNodes)
    {
        // The composer is rendered after the (potentially very large) message
        // history. A reverse depth-first walk reaches the input surface without
        // traversing every historical message first.
        var stack = new Stack<object>();
        stack.Push(root);
        var visited = 0;
        while (stack.Count > 0 && visited < maxNodes)
        {
            var current = stack.Pop();
            visited += 1;
            dynamic accessible = current;
            try
            {
                var role = Convert.ToInt32(accessible.accRole(0));
                var name = Convert.ToString(accessible.accName(0));
                if (role == expectedRole && Array.IndexOf(acceptedNames, name) >= 0)
                {
                    return current;
                }
            }
            catch { }

            int count;
            try { count = Convert.ToInt32(accessible.accChildCount); }
            catch { continue; }
            for (var index = 1; index <= count; index++)
            {
                object child;
                try { child = accessible.accChild(index); }
                catch { continue; }
                if (child != null && !(child is int))
                {
                    stack.Push(child);
                }
            }
        }
        return null;
    }

    public static object FindAccessibleByPrefix(
        object root,
        int expectedRole,
        string[] acceptedPrefixes,
        int maxNodes)
    {
        var stack = new Stack<object>();
        stack.Push(root);
        var visited = 0;
        while (stack.Count > 0 && visited < maxNodes)
        {
            var current = stack.Pop();
            visited += 1;
            dynamic accessible = current;
            try
            {
                var role = Convert.ToInt32(accessible.accRole(0));
                var name = Convert.ToString(accessible.accName(0));
                if (role == expectedRole && Array.Exists(
                    acceptedPrefixes,
                    prefix => name.StartsWith(prefix, StringComparison.Ordinal)))
                {
                    return current;
                }
            }
            catch { }

            int count;
            try { count = Convert.ToInt32(accessible.accChildCount); }
            catch { continue; }
            for (var index = 1; index <= count; index++)
            {
                object child;
                try { child = accessible.accChild(index); }
                catch { continue; }
                if (child != null && !(child is int)) stack.Push(child);
            }
        }
        return null;
    }

    public static bool GetScreenReaderFlag()
    {
        bool value = false;
        if (!SystemParametersInfo(0x0046, 0, ref value, 0))
        {
            throw new System.ComponentModel.Win32Exception();
        }
        return value;
    }

    public static void SetScreenReaderFlag(bool enabled)
    {
        bool value = enabled;
        if (!SystemParametersInfo(0x0047, enabled ? 1u : 0u, ref value, 0x0003))
        {
            throw new System.ComponentModel.Win32Exception();
        }
    }

    private static INPUT Key(ushort virtualKey, uint flags)
    {
        return new INPUT
        {
            type = 1,
            U = new INPUTUNION
            {
                ki = new KEYBDINPUT
                {
                    wVk = virtualKey,
                    wScan = 0,
                    dwFlags = flags,
                    time = 0,
                    dwExtraInfo = UIntPtr.Zero
                }
            }
        };
    }

    public static void SendEnter()
    {
        SendKey(0x0D);
    }

    public static void SendKey(ushort virtualKey)
    {
        var inputs = new[] { Key(virtualKey, 0), Key(virtualKey, 0x0002) };
        if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>()) != inputs.Length)
        {
            throw new System.ComponentModel.Win32Exception();
        }
    }

    public static void SendPaste()
    {
        var inputs = new[]
        {
            Key(0x11, 0),
            Key(0x56, 0),
            Key(0x56, 0x0002),
            Key(0x11, 0x0002)
        };
        if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>()) != inputs.Length)
        {
            throw new System.ComponentModel.Win32Exception();
        }
    }

    public static void SendSelectAll()
    {
        var inputs = new[]
        {
            Key(0x11, 0),
            Key(0x41, 0),
            Key(0x41, 0x0002),
            Key(0x11, 0x0002)
        };
        if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>()) != inputs.Length)
        {
            throw new System.ComponentModel.Win32Exception();
        }
    }

    public static bool ForceForeground(IntPtr window)
    {
        uint ignored;
        var currentThread = GetCurrentThreadId();
        var targetThread = GetWindowThreadProcessId(window, out ignored);
        var foreground = GetForegroundWindow();
        var foregroundThread = foreground == IntPtr.Zero
            ? 0u
            : GetWindowThreadProcessId(foreground, out ignored);
        var attachedForeground = foregroundThread != 0 && foregroundThread != currentThread
            && AttachThreadInput(currentThread, foregroundThread, true);
        var attachedTarget = targetThread != 0 && targetThread != currentThread
            && AttachThreadInput(currentThread, targetThread, true);
        try
        {
            ShowWindow(window, 5);
            BringWindowToTop(window);
            SetForegroundWindow(window);
            return GetForegroundWindow() == window;
        }
        finally
        {
            if (attachedTarget) AttachThreadInput(currentThread, targetThread, false);
            if (attachedForeground) AttachThreadInput(currentThread, foregroundThread, false);
        }
    }

}
'@

function Decode-Utf8Base64 {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) {
        return ''
    }
    return [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

function Get-AccessibleProperty {
    param($Accessible, [string]$Property, [int]$ChildId = 0)
    try {
        switch ($Property) {
            'name' { return $Accessible.accName($ChildId) }
            'role' { return $Accessible.accRole($ChildId) }
            'state' { return $Accessible.accState($ChildId) }
            'value' { return $Accessible.accValue($ChildId) }
            'action' { return $Accessible.accDefaultAction($ChildId) }
        }
    }
    catch {}
    return $null
}

function Find-CodexRenderer {
    param([int]$ProcessId)

    foreach ($windowHandle in [CodexDesktopNative]::TopLevelWindows([uint32]$ProcessId)) {
        foreach ($childHandle in [CodexDesktopNative]::Children($windowHandle)) {
            if ([CodexDesktopNative]::ClassName($childHandle) -ne 'Chrome_RenderWidgetHostHWND') {
                continue
            }
            $accessible = [CodexDesktopNative]::Accessible($childHandle)
            if (-not $accessible) {
                continue
            }
            $value = Get-AccessibleProperty $accessible 'value'
            if ($value -notlike '*initialRoute=*') {
                return [pscustomobject]@{
                    WindowHandle = $windowHandle
                    Renderer = $accessible
                }
            }
        }
    }
    return $null
}

function Find-Composer {
    param($Renderer)
    return [CodexDesktopNative]::FindAccessible(
        $Renderer,
        42,
        @(
            (Decode-Utf8Base64 '6ZqP5b+D6L6T5YWl'),
            'Ask anything',
            (Decode-Utf8Base64 '5o+P6L+w5L2g55qE55uu5qCH77yM5a6a5LmJ5Y+v6KGh6YeP55qE5oiQ5p6c77yM5Lul6I635b6X5pyA5L2z5pWI5p6c'),
            'Describe your goal and define measurable outcomes for the best results'),
        10000)
}

function Find-ButtonByName {
    param($Renderer, [string[]]$Names)
    return [CodexDesktopNative]::FindAccessible($Renderer, 43, $Names, 10000)
}

function Test-AccessibleActionable {
    param($Accessible)
    if (-not $Accessible) {
        return $false
    }
    $state = Get-AccessibleProperty $Accessible 'state'
    if ($null -eq $state) {
        return $false
    }
    # MSAA STATE_SYSTEM_UNAVAILABLE, INVISIBLE, and OFFSCREEN.  The Codex
    # attachment composer exposes its Send button before the pasted image has
    # finished staging, but marks it unavailable.  Invoking the default action
    # at that point is a no-op and must not be reported as a submission.
    $blockedStates = 0x00000001 -bor 0x00008000 -bor 0x00010000
    return (([int]$state -band $blockedStates) -eq 0)
}

function Find-DraftAttachmentRemoveButton {
    param($Renderer)
    return [CodexDesktopNative]::FindAccessibleByPrefix(
        $Renderer,
        43,
        @(
            (Decode-Utf8Base64 '56e76Zmk4oCc'),
            'Remove "',
            'Remove '),
        10000)
}

function Remove-DraftAttachments {
    param($Renderer, [int]$MaxAttachments = 20)
    for ($index = 0; $index -lt $MaxAttachments; $index++) {
        $remove = Find-DraftAttachmentRemoveButton $Renderer
        if (-not $remove) {
            return
        }
        $remove.accDoDefaultAction(0)
        Start-Sleep -Milliseconds 100
    }
    throw 'Codex draft has too many attachments to clear safely.'
}

function Set-AccessibleValue {
    param($Accessible, [string]$Value)
    try {
        $Accessible.set_accValue(0, $Value)
        return
    }
    catch {
        try {
            $Accessible.accValue(0) = $Value
            return
        }
        catch {
            throw 'Codex composer does not accept the accessibility value operation.'
        }
    }
}

function Copy-ClipboardDataObject {
    $source = [System.Windows.Forms.Clipboard]::GetDataObject()
    if (-not $source) {
        return $null
    }
    $copy = New-Object System.Windows.Forms.DataObject
    foreach ($format in $source.GetFormats($false)) {
        try {
            $data = $source.GetData($format, $false)
            if ($null -ne $data) {
                $copy.SetData($format, $data)
            }
        }
        catch {}
    }
    return $copy
}

function Clear-ComposerText {
    param($Composer, [IntPtr]$WindowHandle)
    if (-not [CodexDesktopNative]::ForceForeground($WindowHandle)) {
        throw 'Codex window could not be activated for draft clearing.'
    }
    $Composer.accSelect(1, 0)
    Start-Sleep -Milliseconds 100
    [CodexDesktopNative]::SendSelectAll()
    [CodexDesktopNative]::SendKey(0x08)
}

function Paste-ComposerText {
    param(
        $Renderer,
        [IntPtr]$WindowHandle,
        [string]$Text,
        [datetime]$Deadline
    )
    if ([string]::IsNullOrEmpty($Text)) {
        return
    }
    $clipboardBackup = Copy-ClipboardDataObject
    [System.Windows.Forms.Clipboard]::SetText(
        $Text,
        [System.Windows.Forms.TextDataFormat]::UnicodeText
    )
    $textClipboardSequence = [CodexDesktopNative]::GetClipboardSequenceNumber()
    try {
        if (-not [CodexDesktopNative]::ForceForeground($WindowHandle)) {
            throw 'Codex window could not be activated for text paste.'
        }
        $composer = Find-Composer $Renderer
        if (-not $composer) {
            throw 'Codex composer disappeared before text paste.'
        }
        $composer.accSelect(1, 0)
        Start-Sleep -Milliseconds 100
        [CodexDesktopNative]::SendPaste()

        $pasted = $false
        while ((Get-Date) -lt $Deadline -and -not $pasted) {
            Start-Sleep -Milliseconds 100
            $value = Get-AccessibleProperty $composer 'value'
            $pasted = [string]$value -eq $Text
        }
        if (-not $pasted) {
            throw 'Codex composer did not expose the pasted text before timeout.'
        }
    }
    finally {
        if ([CodexDesktopNative]::GetClipboardSequenceNumber() -eq $textClipboardSequence) {
            if ($clipboardBackup) {
                [System.Windows.Forms.Clipboard]::SetDataObject($clipboardBackup, $true)
            }
            else {
                [System.Windows.Forms.Clipboard]::Clear()
            }
        }
    }
}

function Attach-Files {
    param(
        $Renderer,
        [IntPtr]$WindowHandle,
        [int]$ProcessId,
        [string[]]$Paths,
        [datetime]$Deadline
    )
    if ($Paths.Count -eq 0) {
        return
    }
    $files = New-Object System.Collections.Specialized.StringCollection
    foreach ($path in $Paths) {
        if (-not [System.IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Attachment path is unavailable: $path"
        }
        [void]$files.Add($path)
    }

    $clipboardBackup = Copy-ClipboardDataObject
    [System.Windows.Forms.Clipboard]::SetFileDropList($files)
    $attachmentClipboardSequence = [CodexDesktopNative]::GetClipboardSequenceNumber()
    try {
        if (-not [CodexDesktopNative]::ForceForeground($WindowHandle)) {
            throw 'Codex window could not be activated for attachment paste.'
        }
        $composer = Find-Composer $Renderer
        if (-not $composer) {
            throw 'Codex composer disappeared before attachment paste.'
        }
        $composer.accSelect(1, 0)
        Start-Sleep -Milliseconds 100
        [CodexDesktopNative]::SendPaste()
        # Pasting a file only queues attachment staging.  Do not continue until
        # the attachment is represented in the draft; otherwise the following
        # Send action can submit text alone or do nothing while upload is busy.
        $attachmentReady = $false
        while ((Get-Date) -lt $Deadline -and -not $attachmentReady) {
            Start-Sleep -Milliseconds 100
            $surface = Find-CodexRenderer -ProcessId $ProcessId
            if ($surface) {
                $attachmentReady = $null -ne (Find-DraftAttachmentRemoveButton $surface.Renderer)
            }
        }
        if (-not $attachmentReady) {
            throw 'Codex did not finish staging the draft attachment before timeout.'
        }
    }
    finally {
        if ([CodexDesktopNative]::GetClipboardSequenceNumber() -eq $attachmentClipboardSequence) {
            if ($clipboardBackup) {
                [System.Windows.Forms.Clipboard]::SetDataObject($clipboardBackup, $true)
            }
            else {
                [System.Windows.Forms.Clipboard]::Clear()
            }
        }
    }
}

$result = [ordered]@{
    ok = $false
    action = $Action
    threadId = $ThreadId
    submitted = $false
    usedForegroundFallback = $false
}

$originalScreenReaderFlag = [CodexDesktopNative]::GetScreenReaderFlag()
try {
    if (-not $originalScreenReaderFlag) {
        [CodexDesktopNative]::SetScreenReaderFlag($true)
    }
    Start-Process -FilePath ("codex://threads/{0}" -f $ThreadId)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $surface = $null
    $composer = $null
    while ((Get-Date) -lt $deadline -and -not $composer) {
        Start-Sleep -Milliseconds 100
        $process = Get-Process ChatGPT |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            Select-Object -First 1
        if (-not $process) {
            continue
        }
        $surface = Find-CodexRenderer -ProcessId $process.Id
        if ($surface) {
            $composer = Find-Composer $surface.Renderer
        }
    }
    if (-not $surface -or -not $composer) {
        throw 'Codex desktop composer was not found after task navigation.'
    }
    $goalComposerName = Decode-Utf8Base64 '5o+P6L+w5L2g55qE55uu5qCH77yM5a6a5LmJ5Y+v6KGh6YeP55qE5oiQ5p6c77yM5Lul6I635b6X5pyA5L2z5pWI5p6c'
    if ((Get-AccessibleProperty $composer 'name') -in @(
        $goalComposerName,
        'Describe your goal and define measurable outcomes for the best results')) {
        $goalButton = Find-ButtonByName $surface.Renderer @(
            (Decode-Utf8Base64 '5piO56Gu55uu5qCH'),
            'Define goal')
        if (-not $goalButton) {
            throw 'Codex task is in goal mode and its mode control is unavailable.'
        }
        $goalButton.accDoDefaultAction(0)
        $composer = $null
        $normalDeadline = (Get-Date).AddSeconds(3)
        while ((Get-Date) -lt $normalDeadline -and -not $composer) {
            Start-Sleep -Milliseconds 100
            $surface = Find-CodexRenderer -ProcessId $process.Id
            if ($surface) {
                $candidate = Find-Composer $surface.Renderer
                if ($candidate -and (Get-AccessibleProperty $candidate 'name') -notin @(
                    $goalComposerName,
                    'Describe your goal and define measurable outcomes for the best results')) {
                    $composer = $candidate
                }
            }
        }
        if (-not $composer) {
            throw 'Codex task could not return to normal message mode.'
        }
    }

    $text = Decode-Utf8Base64 $TextBase64
    $attachmentJson = Decode-Utf8Base64 $AttachmentsBase64
    $attachments = @()
    if ($attachmentJson) {
        $decodedAttachments = ConvertFrom-Json -InputObject $attachmentJson
        foreach ($attachment in $decodedAttachments) {
            $attachments += [string]$attachment
        }
    }

    if ($Action -eq 'stop') {
        $stop = Find-ButtonByName $surface.Renderer @((Decode-Utf8Base64 '5YGc5q2i'), 'Stop')
        if (-not $stop) {
            throw 'Codex task has no active stop control.'
        }
        $stop.accDoDefaultAction(0)
        $result.ok = $true
    }
    elseif ($Action -eq 'clear') {
        Clear-ComposerText -Composer $composer -WindowHandle $surface.WindowHandle
        Remove-DraftAttachments $surface.Renderer
        $result.ok = $true
    }
    else {
        Clear-ComposerText -Composer $composer -WindowHandle $surface.WindowHandle
        Remove-DraftAttachments $surface.Renderer
        Attach-Files -Renderer $surface.Renderer -WindowHandle $surface.WindowHandle -ProcessId $process.Id -Paths $attachments -Deadline $deadline
        Paste-ComposerText -Renderer $surface.Renderer -WindowHandle $surface.WindowHandle -Text $text -Deadline $deadline
        if ($Action -eq 'draft') {
            $result.ok = $true
        }
        else {
            $send = $null
            while ((Get-Date) -lt $deadline -and -not $send) {
                Start-Sleep -Milliseconds 100
                $surface = Find-CodexRenderer -ProcessId $process.Id
                if (-not $surface) {
                    continue
                }
                $candidate = Find-ButtonByName $surface.Renderer @(
                    (Decode-Utf8Base64 '5Y+R6YCB'),
                    'Send')
                if (Test-AccessibleActionable $candidate) {
                    $send = $candidate
                }
            }
            if (-not $send -and $attachments.Count -gt 0) {
                throw 'Codex Send control did not become available for the staged attachment.'
            }
            # Chromium exposes the Send control through MSAA while a turn is
            # active, but accDoDefaultAction can return successfully without
            # dispatching the draft. Use its actionable state only as the
            # readiness gate, then submit through the focused composer.
            $composer = Find-Composer $surface.Renderer
            if (-not $composer) {
                throw 'Codex composer disappeared before Enter submission.'
            }
            $composer.accSelect(1, 0)
            if (-not [CodexDesktopNative]::ForceForeground($surface.WindowHandle)) {
                throw 'Codex window could not be activated for Enter submission.'
            }
            Start-Sleep -Milliseconds 100
            [CodexDesktopNative]::SendEnter()
            $result.usedForegroundFallback = $true
            $result.ok = $true
            $result.submitted = $true
        }
    }
}
catch {
    $result.error = $_.Exception.Message
}
finally {
    if (-not $originalScreenReaderFlag) {
        try { [CodexDesktopNative]::SetScreenReaderFlag($false) } catch {}
    }
}

$result | ConvertTo-Json -Compress
if (-not $result.ok) {
    exit 1
}
