param([string]$InstanceId = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not $InstanceId) { $InstanceId = [Guid]::NewGuid().ToString('N').Substring(0, 8) }
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = "EDR-WD Native Recording QA [$InstanceId]"
$form.Name = 'RecordingFixture'
# 20261026: height 420 for the row-container column (region assertion list semantics).
$form.Size = New-Object System.Drawing.Size(640, 420)
$form.StartPosition = 'CenterScreen'
$inputBox = New-Object System.Windows.Forms.TextBox
$inputBox.Name = 'FixtureInput'
$inputBox.AccessibleName = 'QA input'
$inputBox.Text = 'baseline'
$inputBox.SetBounds(20, 20, 400, 30)
$check = New-Object System.Windows.Forms.CheckBox
$check.Name = 'FixtureCheck'
$check.AccessibleName = 'QA enabled'
$check.Text = 'QA enabled'
$check.SetBounds(20, 65, 200, 30)
$save = New-Object System.Windows.Forms.Button
$save.Name = 'FixtureSave'
$save.AccessibleName = 'Save sample'
$save.Text = 'Save sample'
$save.SetBounds(20, 110, 150, 32)
$reset = New-Object System.Windows.Forms.Button
$reset.Name = 'FixtureReset'
$reset.AccessibleName = 'Reset sample'
$reset.Text = 'Reset sample'
$reset.SetBounds(190, 110, 150, 32)
$status = New-Object System.Windows.Forms.Label
$status.Name = 'FixtureStatus'
# Let UIA expose the changing result text, not a constant accessible label.
# FixtureStatus remains the stable AutomationId used to locate this control.
$status.Text = 'ready'
$status.SetBounds(20, 160, 420, 35)
$save.Add_Click({
    $status.Text = 'saved:' + $inputBox.Text + ':' + $check.Checked.ToString().ToLowerInvariant()
})
$reset.Add_Click({
    $inputBox.Text = 'baseline'
    $check.Checked = $false
    $status.Text = 'ready'
})
# Row container (20261026 region assertion acceptance): ListBox exposes rows to
# UIA as children; Add/Shuffle drive first_match and absent_full_period cases.
$listBox = New-Object System.Windows.Forms.ListBox
$listBox.Name = 'FixtureList'
$listBox.AccessibleName = 'QA rows'
$listBox.SetBounds(450, 20, 170, 300)
[void]$listBox.Items.Add('item-alpha')
[void]$listBox.Items.Add('item-beta')
[void]$listBox.Items.Add('item-gamma')
$addRow = New-Object System.Windows.Forms.Button
$addRow.Name = 'FixtureAddRow'
$addRow.AccessibleName = 'Add row'
$addRow.Text = 'Add row'
$addRow.SetBounds(20, 210, 150, 32)
$addRow.Add_Click({
    [void]$listBox.Items.Add('item-' + [Guid]::NewGuid().ToString('N').Substring(0, 6))
})
$shuffle = New-Object System.Windows.Forms.Button
$shuffle.Name = 'FixtureShuffle'
$shuffle.AccessibleName = 'Shuffle rows'
$shuffle.Text = 'Shuffle rows'
$shuffle.SetBounds(190, 210, 150, 32)
$shuffle.Add_Click({
    if ($listBox.Items.Count -gt 0) { $listBox.Items.RemoveAt(0) }
})
$status.SetBounds(20, 260, 420, 35)
$form.Controls.AddRange(@($inputBox, $check, $save, $reset, $status, $listBox, $addRow, $shuffle))
Write-Output "FIXTURE_TITLE=$($form.Text)"
Write-Output "FIXTURE_PID=$PID"
[System.Windows.Forms.Application]::Run($form)
