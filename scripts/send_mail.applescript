on run argv
    if (count of argv) is less than 5 then
        error "Usage: recipient sender subject body attachment"
    end if

    set recipientAddresses to item 1 of argv
    set senderAddress to item 2 of argv
    set messageSubject to item 3 of argv
    set messageBody to item 4 of argv
    set attachmentPath to item 5 of argv
    set AppleScript's text item delimiters to ","
    set recipientList to text items of recipientAddresses
    set AppleScript's text item delimiters to ""

    tell application "Mail"
        set outgoingMessage to make new outgoing message with properties {subject:messageSubject, content:messageBody & return & return, visible:false, sender:senderAddress}
        tell outgoingMessage
            repeat with recipientAddress in recipientList
                set cleanedAddress to my trimText(recipientAddress as text)
                if cleanedAddress is not "" then
                    make new to recipient at end of to recipients with properties {address:cleanedAddress}
                end if
            end repeat
            if attachmentPath is not "" then
                make new attachment with properties {file name:(POSIX file attachmentPath)} at after the last paragraph
            end if
            send
        end tell
    end tell

    return "sent"
end run

on trimText(theText)
    if theText is "" then return ""
    set whiteSpace to {" ", tab, return, linefeed}
    repeat while whiteSpace contains first character of theText
        if (length of theText) is 1 then return ""
        set theText to text 2 thru -1 of theText
    end repeat
    repeat while whiteSpace contains last character of theText
        if (length of theText) is 1 then return ""
        set theText to text 1 thru -2 of theText
    end repeat
    return theText
end trimText
