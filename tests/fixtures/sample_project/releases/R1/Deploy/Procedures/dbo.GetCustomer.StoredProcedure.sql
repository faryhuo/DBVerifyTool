CREATE OR ALTER PROCEDURE dbo.GetCustomer @Id INT
AS
BEGIN
    DECLARE @x INT;
    DECLARE @x INT;
    SELECT Id, Name FROM dbo.Customer WHERE Id = @Id AND Name <> "abc";
END
GO
