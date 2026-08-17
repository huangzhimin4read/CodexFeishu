param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('inspect', 'draft', 'clear', 'send-draft', 'submit', 'stop')]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$ThreadId,

    [string]$TextBase64 = '',

    [string]$AttachmentsBase64 = '',

    [int]$TimeoutSeconds = 15,

    [switch]$BackgroundOnly,

    [string]$ExpectedThreadTitleBase64 = ''
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
    [ComImport]
    [Guid("A59AA09A-7011-4B65-939D-32B1FB5547E3")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IAccessibleEditableText
    {
        [PreserveSig]
        int copyText(int startOffset, int endOffset);

        [PreserveSig]
        int deleteText(int startOffset, int endOffset);

        [PreserveSig]
        int insertText(
            int offset,
            [In, MarshalAs(UnmanagedType.BStr)] ref string text);

        [PreserveSig]
        int cutText(int startOffset, int endOffset);

        [PreserveSig]
        int pasteText(int offset);

        [PreserveSig]
        int replaceText(
            int startOffset,
            int endOffset,
            [In, MarshalAs(UnmanagedType.BStr)] ref string text);

        [PreserveSig]
        int setAttributes(
            int startOffset,
            int endOffset,
            [In, MarshalAs(UnmanagedType.BStr)] ref string attributes);
    }

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
    public static extern bool PostMessage(
        IntPtr hWnd,
        uint msg,
        IntPtr wParam,
        IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern uint MapVirtualKey(uint code, uint mapType);

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

    public static int ReplaceAccessibleText(
        object accessible,
        int currentLength,
        string replacement)
    {
        IntPtr unknown = IntPtr.Zero;
        IntPtr editablePointer = IntPtr.Zero;
        object editableObject = null;
        try
        {
            unknown = Marshal.GetIUnknownForObject(accessible);
            var iid = new Guid("A59AA09A-7011-4B65-939D-32B1FB5547E3");
            var queryResult = Marshal.QueryInterface(
                unknown,
                ref iid,
                out editablePointer);
            if (queryResult < 0 || editablePointer == IntPtr.Zero)
            {
                return queryResult;
            }
            editableObject = Marshal.GetTypedObjectForIUnknown(
                editablePointer,
                typeof(IAccessibleEditableText));
            var editable = (IAccessibleEditableText)editableObject;
            var text = replacement ?? String.Empty;
            return editable.replaceText(0, currentLength, ref text);
        }
        finally
        {
            if (editableObject != null && Marshal.IsComObject(editableObject))
            {
                Marshal.ReleaseComObject(editableObject);
            }
            if (editablePointer != IntPtr.Zero)
            {
                Marshal.Release(editablePointer);
            }
            if (unknown != IntPtr.Zero)
            {
                Marshal.Release(unknown);
            }
        }
    }

    private static IntPtr KeyMessageData(ushort virtualKey, bool released)
    {
        var scanCode = MapVirtualKey(virtualKey, 0);
        long value = 1L | ((long)scanCode << 16);
        if (released)
        {
            value |= 1L << 30;
            value |= 1L << 31;
        }
        return new IntPtr(value);
    }

    private static bool PostKey(IntPtr window, ushort virtualKey, bool released)
    {
        return PostMessage(
            window,
            released ? 0x0101u : 0x0100u,
            new IntPtr(virtualKey),
            KeyMessageData(virtualKey, released));
    }

    public static bool PostReplaceText(IntPtr window, string replacement)
    {
        // Each Chromium renderer preserves its own focused DOM node while its
        // top-level window is inactive. Posting directly to that renderer does
        // not alter the system foreground window.
        var ok = PostKey(window, 0x11, false)
            && PostKey(window, 0x41, false)
            && PostKey(window, 0x41, true)
            && PostKey(window, 0x11, true)
            && PostKey(window, 0x08, false)
            && PostKey(window, 0x08, true);
        if (!ok)
        {
            return false;
        }
        foreach (var character in replacement ?? String.Empty)
        {
            ok = PostMessage(
                window,
                0x0102u,
                new IntPtr(character),
                new IntPtr(1)) && ok;
        }
        return ok;
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

    public static bool HasSelectedAccessibleName(
        object root,
        string expectedName,
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
                var name = Convert.ToString(accessible.accName(0));
                var state = Convert.ToInt32(accessible.accState(0));
                if (String.Equals(name, expectedName, StringComparison.Ordinal)
                    && (state & 0x00000002) != 0)
                {
                    return true;
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
        return false;
    }

    public static string[] AccessibleNameDiagnostics(
        object root,
        string expectedName,
        int maxNodes)
    {
        var result = new List<string>();
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
                var name = Convert.ToString(accessible.accName(0));
                if (String.Equals(name, expectedName, StringComparison.Ordinal))
                {
                    var role = Convert.ToInt32(accessible.accRole(0));
                    var state = Convert.ToInt32(accessible.accState(0));
                    string action;
                    try { action = Convert.ToString(accessible.accDefaultAction(0)); }
                    catch { action = String.Empty; }
                    result.Add(String.Format("role={0};state={1};action={2}", role, state, action));
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
        return result.ToArray();
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
                    RendererHandle = $childHandle
                    Renderer = $accessible
                }
            }
        }
    }
    return $null
}

function Find-CodexRendererBySelectedTitle {
    param(
        [int]$ProcessId,
        [string]$ExpectedTitle,
        [AllowNull()][string]$ExpectedComposerValue = $null
    )

    $selectedMatches = @()
    $uniqueTitleCandidates = @()
    $diagnostics = @()
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
            if ($value -like '*initialRoute=*') {
                continue
            }
            $nameDiagnostics = @([CodexDesktopNative]::AccessibleNameDiagnostics(
                $accessible,
                $ExpectedTitle,
                20000))
            if ($nameDiagnostics.Count -eq 0) {
                continue
            }
            $composer = Find-Composer $accessible
            if (-not $composer) {
                continue
            }
            if ($null -ne $ExpectedComposerValue -and
                [string](Get-AccessibleProperty $composer 'value') -ne $ExpectedComposerValue) {
                continue
            }
            $surface = [pscustomobject]@{
                WindowHandle = $windowHandle
                RendererHandle = $childHandle
                Renderer = $accessible
            }
            $uniqueTitleCandidates += $surface
            $diagnostics += "window=$windowHandle[$($nameDiagnostics -join ',')]"
            if ([CodexDesktopNative]::HasSelectedAccessibleName(
                $accessible,
                $ExpectedTitle,
                20000)) {
                $selectedMatches += $surface
            }
        }
    }
    if ($selectedMatches.Count -eq 1) {
        return $selectedMatches[0]
    }
    if ($selectedMatches.Count -eq 0 -and $uniqueTitleCandidates.Count -eq 1) {
        return $uniqueTitleCandidates[0]
    }
    $detail = if ($diagnostics.Count -eq 0) { 'none' } else { $diagnostics -join '|' }
    throw "Expected exactly one background Codex window for task '$ExpectedTitle'; selected=$($selectedMatches.Count), exact-title=$($uniqueTitleCandidates.Count), diagnostics=$detail."
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
    param($Accessible, [IntPtr]$RendererHandle, [string]$Value)
    $current = [string](Get-AccessibleProperty $Accessible 'value')
    $replaceResult = [CodexDesktopNative]::ReplaceAccessibleText(
        $Accessible,
        $current.Length,
        $Value)
    if ($replaceResult -ge 0 -and
        [string](Get-AccessibleProperty $Accessible 'value') -eq $Value) {
        return
    }
    $posted = [CodexDesktopNative]::PostReplaceText($RendererHandle, $Value)
    if ($posted) {
        Start-Sleep -Milliseconds 200
        if ([string](Get-AccessibleProperty $Accessible 'value') -eq $Value) {
            return
        }
    }
    $legacyError = ''
    try {
        $Accessible.set_accValue(0, $Value)
    }
    catch {
        try {
            $Accessible.accValue(0) = $Value
        }
        catch {
            $legacyError = $_.Exception.Message
        }
    }
    if ([string](Get-AccessibleProperty $Accessible 'value') -eq $Value) {
        return
    }
    $replaceUnsigned = [BitConverter]::ToUInt32(
        [BitConverter]::GetBytes([int32]$replaceResult),
        0)
    $replaceHex = ('0x{0:X8}' -f $replaceUnsigned)
    throw "Codex composer rejected background text editing (IAccessible2=$replaceHex; postMessage=$posted; legacy=$legacyError)."
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
    param(
        $Composer,
        [IntPtr]$WindowHandle,
        [IntPtr]$AutomationHandle,
        [bool]$UseBackgroundAccess
    )
    if ($UseBackgroundAccess) {
        Set-AccessibleValue -Accessible $Composer -RendererHandle $AutomationHandle -Value ''
        if (-not [string]::IsNullOrEmpty([string](Get-AccessibleProperty $Composer 'value'))) {
            throw 'Codex composer did not clear through background accessibility.'
        }
        return
    }
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
        [IntPtr]$AutomationHandle,
        [string]$Text,
        [datetime]$Deadline,
        [bool]$UseBackgroundAccess
    )
    if ([string]::IsNullOrEmpty($Text)) {
        return
    }
    if ($UseBackgroundAccess) {
        $composer = Find-Composer $Renderer
        if (-not $composer) {
            throw 'Codex composer disappeared before background text input.'
        }
        Set-AccessibleValue -Accessible $composer -RendererHandle $AutomationHandle -Value $Text
        $value = Get-AccessibleProperty $composer 'value'
        if ([string]$value -ne $Text) {
            throw 'Codex composer did not expose the background text value.'
        }
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
        [datetime]$Deadline,
        [bool]$UseBackgroundAccess
    )
    if ($Paths.Count -eq 0) {
        return
    }
    if ($UseBackgroundAccess) {
        throw 'Background Codex input does not support direct draft attachments.'
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
    backgroundOnly = [bool]$BackgroundOnly
    composerTextLength = $null
    windowHandle = $null
}

$originalScreenReaderFlag = [CodexDesktopNative]::GetScreenReaderFlag()
try {
    if (-not $originalScreenReaderFlag) {
        [CodexDesktopNative]::SetScreenReaderFlag($true)
    }
    $expectedThreadTitle = Decode-Utf8Base64 $ExpectedThreadTitleBase64
    if ($BackgroundOnly -and [string]::IsNullOrWhiteSpace($expectedThreadTitle)) {
        throw 'Background Codex input requires the expected task title.'
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
    $backgroundPrefilledSubmit = $BackgroundOnly -and $Action -eq 'submit'
    if ($backgroundPrefilledSubmit) {
        if ($attachments.Count -gt 0) {
            throw 'Background relay submission carries attachment paths in text and cannot stage direct draft attachments.'
        }
        if ([string]::IsNullOrEmpty($text)) {
            throw 'Background relay submission requires a non-empty prompt.'
        }
        # Codex Desktop's local-conversation deep link supports a prompt query
        # that the app converts to prefillPrompt. Pure deep-link second-instance
        # arguments are routed without the main-process foreground path. This
        # gives the renderer its normal React input events without clipboard or
        # simulated global keyboard input.
        $promptUri = "codex://threads/{0}?prompt={1}" -f (
            $ThreadId,
            [Uri]::EscapeDataString($text))
        Start-Process -FilePath $promptUri
    }
    elseif (-not $BackgroundOnly) {
        Start-Process -FilePath ("codex://threads/{0}" -f $ThreadId)
    }
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
        $surface = if ($backgroundPrefilledSubmit) {
            try {
                Find-CodexRendererBySelectedTitle `
                    -ProcessId $process.Id `
                    -ExpectedTitle $expectedThreadTitle `
                    -ExpectedComposerValue $text
            }
            catch {
                $null
            }
        }
        elseif ($BackgroundOnly) {
            Find-CodexRendererBySelectedTitle `
                -ProcessId $process.Id `
                -ExpectedTitle $expectedThreadTitle
        }
        else {
            Find-CodexRenderer -ProcessId $process.Id
        }
        if ($surface) {
            $composer = Find-Composer $surface.Renderer
        }
    }
    if (-not $surface -or -not $composer) {
        throw 'Codex desktop composer was not found after task navigation.'
    }
    $result.windowHandle = $surface.WindowHandle.ToInt64()
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

    if ($BackgroundOnly -and $Action -in @('send-draft', 'submit')) {
        $activeStop = Find-ButtonByName $surface.Renderer @(
            (Decode-Utf8Base64 '5YGc5q2i'),
            'Stop')
        if (Test-AccessibleActionable $activeStop) {
            throw 'The background Codex relay task is busy.'
        }
    }

    $effectiveAction = if ($backgroundPrefilledSubmit) { 'send-draft' } else { $Action }

    if ($effectiveAction -eq 'inspect') {
        $result.composerTextLength = ([string](Get-AccessibleProperty $composer 'value')).Length
        $result.ok = $true
    }
    elseif ($effectiveAction -eq 'stop') {
        $stop = Find-ButtonByName $surface.Renderer @((Decode-Utf8Base64 '5YGc5q2i'), 'Stop')
        if (-not $stop) {
            throw 'Codex task has no active stop control.'
        }
        $stop.accDoDefaultAction(0)
        $result.ok = $true
    }
    elseif ($effectiveAction -eq 'clear') {
        Clear-ComposerText -Composer $composer -WindowHandle $surface.WindowHandle -AutomationHandle $surface.RendererHandle -UseBackgroundAccess $BackgroundOnly
        Remove-DraftAttachments $surface.Renderer
        $result.ok = $true
    }
    elseif ($effectiveAction -eq 'send-draft') {
        $draftText = [string](Get-AccessibleProperty $composer 'value')
        if ([string]::IsNullOrEmpty($draftText)) {
            throw 'The selected Codex relay task has no draft to submit.'
        }
        $send = Find-ButtonByName $surface.Renderer @(
            (Decode-Utf8Base64 '5Y+R6YCB'),
            'Send')
        if (-not (Test-AccessibleActionable $send)) {
            throw 'Codex Send control is unavailable for the prefilled relay draft.'
        }
        $send.accDoDefaultAction(0)
        if ($backgroundPrefilledSubmit) {
            # Submitting replaces the Chromium accessibility subtree, so the
            # old composer object is not a reliable acknowledgement. The
            # Python dispatcher confirms both the relay user item and the
            # delegated target item from append-only Codex rollout records.
            $result.ok = $true
            $result.submitted = $true
        }
        else {
            $submitted = $false
            while ((Get-Date) -lt $deadline -and -not $submitted) {
                Start-Sleep -Milliseconds 100
                $currentComposer = Find-Composer $surface.Renderer
                if ($currentComposer) {
                    $submitted = [string](Get-AccessibleProperty $currentComposer 'value') -ne $draftText
                }
            }
            if (-not $submitted) {
                throw 'Codex Send control did not accept the prefilled relay draft.'
            }
            $result.ok = $true
            $result.submitted = $true
        }
    }
    else {
        Clear-ComposerText -Composer $composer -WindowHandle $surface.WindowHandle -AutomationHandle $surface.RendererHandle -UseBackgroundAccess $BackgroundOnly
        Remove-DraftAttachments $surface.Renderer
        Attach-Files -Renderer $surface.Renderer -WindowHandle $surface.WindowHandle -ProcessId $process.Id -Paths $attachments -Deadline $deadline -UseBackgroundAccess $BackgroundOnly
        Paste-ComposerText -Renderer $surface.Renderer -WindowHandle $surface.WindowHandle -AutomationHandle $surface.RendererHandle -Text $text -Deadline $deadline -UseBackgroundAccess $BackgroundOnly
        if ($Action -eq 'draft') {
            $result.ok = $true
        }
        else {
            $send = $null
            while ((Get-Date) -lt $deadline -and -not $send) {
                Start-Sleep -Milliseconds 100
                $surface = if ($BackgroundOnly) {
                    Find-CodexRendererBySelectedTitle -ProcessId $process.Id -ExpectedTitle $expectedThreadTitle
                }
                else {
                    Find-CodexRenderer -ProcessId $process.Id
                }
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
            if ($BackgroundOnly) {
                $send.accDoDefaultAction(0)
                $submitted = $false
                while ((Get-Date) -lt $deadline -and -not $submitted) {
                    Start-Sleep -Milliseconds 100
                    $currentComposer = Find-Composer $surface.Renderer
                    if ($currentComposer) {
                        $submitted = [string](Get-AccessibleProperty $currentComposer 'value') -ne $text
                    }
                }
                if (-not $submitted) {
                    throw 'Codex Send control did not accept the background draft.'
                }
            }
            else {
                $composer.accSelect(1, 0)
                if (-not [CodexDesktopNative]::ForceForeground($surface.WindowHandle)) {
                    throw 'Codex window could not be activated for Enter submission.'
                }
                Start-Sleep -Milliseconds 100
                [CodexDesktopNative]::SendEnter()
                $result.usedForegroundFallback = $true
            }
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
