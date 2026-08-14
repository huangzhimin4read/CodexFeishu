$lsaSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

public static class CodexFeishuLsaRights
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_OBJECT_ATTRIBUTES
    {
        public int Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LSA_UNICODE_STRING
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint LsaOpenPolicy(IntPtr systemName,
        ref LSA_OBJECT_ATTRIBUTES objectAttributes, uint desiredAccess,
        out IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaAddAccountRights(IntPtr policyHandle,
        byte[] accountSid, LSA_UNICODE_STRING[] userRights, uint countOfRights);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint status);

    public static void AddRight(string sidValue, string rightName)
    {
        const uint POLICY_CREATE_ACCOUNT = 0x00000010;
        const uint POLICY_LOOKUP_NAMES = 0x00000800;
        var attributes = new LSA_OBJECT_ATTRIBUTES();
        attributes.Length = Marshal.SizeOf(typeof(LSA_OBJECT_ATTRIBUTES));
        IntPtr policy;
        uint status = LsaOpenPolicy(IntPtr.Zero, ref attributes,
            POLICY_CREATE_ACCOUNT | POLICY_LOOKUP_NAMES, out policy);
        if (status != 0)
            throw new Win32Exception((int)LsaNtStatusToWinError(status));

        IntPtr rightBuffer = IntPtr.Zero;
        try
        {
            var sid = new SecurityIdentifier(sidValue);
            var sidBytes = new byte[sid.BinaryLength];
            sid.GetBinaryForm(sidBytes, 0);
            rightBuffer = Marshal.StringToHGlobalUni(rightName);
            var right = new LSA_UNICODE_STRING {
                Buffer = rightBuffer,
                Length = checked((ushort)(rightName.Length * 2)),
                MaximumLength = checked((ushort)((rightName.Length + 1) * 2))
            };
            status = LsaAddAccountRights(policy, sidBytes, new[] { right }, 1);
            if (status != 0)
                throw new Win32Exception((int)LsaNtStatusToWinError(status));
        }
        finally
        {
            if (rightBuffer != IntPtr.Zero) Marshal.FreeHGlobal(rightBuffer);
            LsaClose(policy);
        }
    }
}
'@

if (-not ('CodexFeishuLsaRights' -as [type])) {
    Add-Type -TypeDefinition $lsaSource -Language CSharp
}

function Grant-CodexFeishuAccountRight {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Sid,
        [Parameter(Mandatory = $true)][string]$Right
    )
    [CodexFeishuLsaRights]::AddRight($Sid, $Right)
}
